"""Self-update for the Runway sidecar: download → verify → swap → restart.

This is the *apply* layer that sits on top of the notify-only detection in
``scripts.sidecar_pkg.update_check``. It is shared by the headless CLI
(``scripts/sidecar.py``) and the desktop tray (``sidecar_app``), so — like
``update_check`` — it uses only the standard library (urllib) and never imports
``app.*``; the frozen sidecar binary must stay self-contained.

Safety model:
  * **Frozen-only.** Self-update runs only under PyInstaller
    (``getattr(sys, "frozen", False)``). From-source checkouts and Docker
    containers are no-ops — they update via ``git pull`` / image repull.
  * **Checksum mandatory.** Every release asset ships a sibling ``.sha256``;
    we refuse to install bytes that don't verify.
  * **Download-then-swap.** The installed copy is never touched until the new
    asset is fully downloaded, verified, and extracted, so a failure leaves the
    running install intact.
  * **Single-flight.** An exclusive lock file prevents two updates at once.
  * The previous binary/bundle is renamed aside as ``.old`` — a rollback
    breadcrumb the user can restore by hand if a build misbehaves.

The OS-mutating ``apply_update`` is intentionally a thin per-platform shell and
is verified manually during release QA rather than in unit tests; the pure
parts (asset-name resolution, checksum, frozen/target detection) are tested.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from urllib import error, request

from scripts.sidecar_pkg.update_check import (
    _LATEST_URL,
    check_once,
    parse_channel,
)

logger = logging.getLogger(__name__)

# The `edge` rolling prerelease object (carries assets[]); distinct from
# update_check._EDGE_REFS_URL, which only returns the git ref sha.
_EDGE_RELEASE_URL = "https://api.github.com/repos/s3ntin3l8/runway/releases/tags/edge"
_TIMEOUT_SECONDS = 60
_LOCK_NAME = "self-update.lock"


class SelfUpdateError(Exception):
    """A recoverable self-update failure (network, missing asset, bad checksum)."""


class SelfUpdateUnsupportedError(SelfUpdateError):
    """Self-update is not available for this platform/channel combination."""


# ---------------------------------------------------------------------------
# Environment detection (pure)
# ---------------------------------------------------------------------------


def _is_frozen() -> bool:
    """True when running as a PyInstaller frozen binary."""
    return bool(getattr(sys, "frozen", False))


def _is_docker() -> bool:
    """Best-effort Docker detection — containers update by repulling the image."""
    return pathlib.Path("/.dockerenv").exists()


def _detect_target() -> str:
    """Classify the running binary as ``"cli"`` or ``"tray"``.

    Only the CLI PyInstaller spec names the executable ``runway-sidecar-cli``;
    all three tray specs produce ``RunwaySidecar`` / ``Runway Sidecar.app``.
    """
    exe = os.path.basename(sys.executable).lower()
    return "cli" if "cli" in exe else "tray"


def _sidecar_dir() -> pathlib.Path:
    """Sidecar config dir — mirrors ``scripts.sidecar.get_sidecar_dir`` without
    importing the (large) CLI module, keeping this file self-contained."""
    override = os.getenv("RUNWAY_CONFIG_DIR")
    if override:
        return pathlib.Path(override) / "sidecar"
    if platform.system() == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data:
            return pathlib.Path(app_data) / "runway" / "sidecar"
        return pathlib.Path.home() / "AppData" / "Roaming" / "runway" / "sidecar"
    return pathlib.Path.home() / ".config" / "runway" / "sidecar"


# ---------------------------------------------------------------------------
# Asset resolution (pure — most-tested)
# ---------------------------------------------------------------------------


def resolve_asset_name(target: str, channel: str, version: str | None) -> str:
    """Return the GitHub release asset base name for this platform/target/channel.

    *version* is the latest tag (without ``v``) for the stable channel; the edge
    channel uses a single rolling asset, so *version* is ignored there.

    Raises ``SelfUpdateUnsupportedError`` for combinations with no published asset
    (edge builds are Linux-only).
    """
    plat = sys.platform
    if channel == "edge":
        if plat != "linux":
            raise SelfUpdateUnsupportedError("edge builds are Linux-only")
        suffix = "CLI-edge" if target == "cli" else "edge"
        return f"Runway-Sidecar-Linux-{suffix}.tar.gz"

    ver = (version or "").lstrip("v").strip()
    if not ver:
        raise SelfUpdateError("missing latest version for stable asset name")
    if plat == "darwin":
        return f"Runway-Sidecar-macOS-{ver}.zip"
    if plat == "win32":
        return f"Runway-Sidecar-Windows-{ver}.zip"
    if plat == "linux":
        kind = "Linux-CLI" if target == "cli" else "Linux"
        return f"Runway-Sidecar-{kind}-{ver}.tar.gz"
    raise SelfUpdateUnsupportedError(f"unsupported platform: {plat}")


# ---------------------------------------------------------------------------
# GitHub release JSON + asset URLs
# ---------------------------------------------------------------------------


def _get_json(url: str) -> dict:
    req = request.Request(  # noqa: S310 — fixed https GitHub API URL
        url, headers={"User-Agent": "Runway-Sidecar-SelfUpdate"}
    )
    with request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _get_release_json(channel: str) -> dict:
    """Fetch the release object (with ``assets[]``) for the active channel."""
    return _get_json(_EDGE_RELEASE_URL if channel == "edge" else _LATEST_URL)


def find_asset_urls(release: dict, asset_name: str) -> tuple[str, str]:
    """Return ``(asset_url, sha256_url)`` for *asset_name* within *release*.

    Raises ``SelfUpdateError`` if either the asset or its ``.sha256`` sibling is
    absent — the checksum is mandatory.
    """
    by_name = {a.get("name"): a.get("browser_download_url") for a in release.get("assets", [])}
    asset_url = by_name.get(asset_name)
    sha_url = by_name.get(f"{asset_name}.sha256")
    if not asset_url:
        raise SelfUpdateError(f"release asset not found: {asset_name}")
    if not sha_url:
        raise SelfUpdateError(f"checksum asset not found: {asset_name}.sha256")
    return asset_url, sha_url


# ---------------------------------------------------------------------------
# Download + verify
# ---------------------------------------------------------------------------


def _download(url: str, dest: pathlib.Path) -> None:
    req = request.Request(url, headers={"User-Agent": "Runway-Sidecar-SelfUpdate"})  # noqa: S310
    with request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp, open(dest, "wb") as fh:  # noqa: S310
        shutil.copyfileobj(resp, fh)


def _fetch_expected_sha(url: str) -> str:
    """Download a ``.sha256`` file and return the lowercased hex digest."""
    req = request.Request(url, headers={"User-Agent": "Runway-Sidecar-SelfUpdate"})  # noqa: S310
    with request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
        text = resp.read().decode().strip()
    # shasum format: "<hex>  <filename>" — take the first token.
    return text.split()[0].lower() if text else ""


def verify_sha256(path: pathlib.Path, expected_hex: str) -> bool:
    """True when the SHA-256 of *path* matches *expected_hex* (case-insensitive)."""
    if not expected_hex:
        return False
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower() == expected_hex.lower()


def _is_within(base: pathlib.Path, target: pathlib.Path) -> bool:
    """True when *target* resolves to a path inside *base* (no traversal/escape)."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _extract(archive: pathlib.Path, dest: pathlib.Path) -> None:
    """Extract *archive* into *dest*, rejecting any member that would escape it.

    Defence-in-depth: the archive is already checksum-verified, but we still
    guard against path traversal (zip-slip / tar-slip).
    """
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if not _is_within(dest, dest / name):
                    raise SelfUpdateError(f"unsafe path in archive: {name!r}")
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            # PEP 706 "data" filter blocks absolute paths, "..", and unsafe links.
            tf.extractall(dest, filter="data")


# ---------------------------------------------------------------------------
# Single-flight lock
# ---------------------------------------------------------------------------


@contextmanager
def _single_flight() -> Iterator[bool]:
    """Yield True if we acquired the update lock, False if one is already held.

    Uses ``O_CREAT|O_EXCL`` (same TOCTOU-free pattern as the CLI PID file).
    """
    lock_dir = _sidecar_dir()
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # If we can't even create the dir, treat as un-lockable but proceed.
        yield True
        return
    lock_path = lock_dir / _LOCK_NAME
    fd: int | None = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        yield True
    except FileExistsError:
        logger.warning("Self-update already in progress; skipping")
        yield False
        return
    finally:
        if fd is not None:
            os.close(fd)
            try:
                lock_path.unlink()
            except OSError:
                # Lock file already gone (or not removable); nothing to clean up.
                pass


# ---------------------------------------------------------------------------
# Platform-specific swap + restart
# ---------------------------------------------------------------------------


def _install_path() -> pathlib.Path:
    """The live install target to swap: the one-file exe, or the macOS .app root."""
    exe = pathlib.Path(sys.executable)
    if sys.platform == "darwin":
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
    return exe


def _find_staged(staged_dir: pathlib.Path, install: pathlib.Path) -> pathlib.Path:
    """Locate the new binary/bundle inside the extracted *staged_dir*."""
    if sys.platform == "darwin" and install.suffix == ".app":
        for p in staged_dir.rglob("*.app"):
            return p
        raise SelfUpdateError("no .app bundle found in downloaded archive")
    # One-file binary: match by the installed file's name, else first executable.
    for p in staged_dir.rglob(install.name):
        if p.is_file():
            return p
    for p in staged_dir.rglob("*"):
        if p.is_file() and os.access(p, os.X_OK):
            return p
    raise SelfUpdateError("no binary found in downloaded archive")


def apply_update(target: str, staged_dir: pathlib.Path, *, restart: bool) -> bool:
    """Swap the running install with the staged copy and optionally relaunch.

    Returns True on a successful swap. On a non-writable install path it logs
    and returns False without leaving partial state.
    """
    install = _install_path()
    staged = _find_staged(staged_dir, install)
    old = install.with_name(install.name + ".old")

    if sys.platform == "win32":
        return _apply_windows(install, staged, restart=restart)

    # POSIX (Linux + macOS): rename the running file/bundle aside, move the new
    # one into place. A running file's open inode survives the unlink.
    try:
        if old.exists():
            _rm(old)
        os.rename(install, old)
        shutil.move(str(staged), str(install))
        if install.is_file():
            # Owner-only rwx: the binary is re-exec'd as the user that runs it,
            # so it needs no group/world bits.
            os.chmod(install, 0o700)
    except PermissionError:
        logger.error(
            "Self-update aborted: install path %s is not writable; update manually", install
        )
        # Best-effort restore if we moved the original aside.
        if not install.exists() and old.exists():
            try:
                os.rename(old, install)
            except OSError:
                # Restore is best-effort; nothing more we can do if it also fails.
                pass
        return False
    except OSError:
        logger.exception("Self-update swap failed")
        return False

    logger.info("Installed update at %s (previous kept at %s)", install, old)
    if restart:
        _relaunch_posix(target, install)
    return True


def _apply_windows(install: pathlib.Path, staged: pathlib.Path, *, restart: bool) -> bool:
    """Windows swap: a running .exe can't be overwritten, so a detached helper
    waits for us to exit, replaces the exe, and relaunches."""
    new_exe = install.with_name(install.stem + ".new" + install.suffix)
    try:
        if new_exe.exists():
            _rm(new_exe)
        shutil.move(str(staged), str(new_exe))
    except OSError:
        logger.exception("Self-update staging failed")
        return False

    pid = os.getpid()
    helper = install.with_name("runway-self-update.bat")
    relaunch = f'start "" "{install}"' if restart else "rem no relaunch"
    script = (
        "@echo off\r\n"
        ":waitloop\r\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\r\n'
        "if not errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >NUL\r\n"
        "  goto waitloop\r\n"
        ")\r\n"
        f'move /Y "{new_exe}" "{install}" >NUL\r\n'
        "if errorlevel 1 (\r\n"
        # Replacement failed — relaunch the old exe so the user isn't stranded.
        f"  {relaunch}\r\n"
        '  del "%~f0"\r\n'
        "  exit /b 1\r\n"
        ")\r\n"
        f"{relaunch}\r\n"
        'del "%~f0"\r\n'
    )
    try:
        helper.write_text(script)
    except OSError:
        logger.exception("Self-update helper write failed")
        return False

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(  # noqa: S603
        ["cmd.exe", "/c", str(helper)],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    logger.info("Self-update staged; helper will swap %s after exit", install)
    if restart:
        # The helper relaunches once we exit; leave the loop / process to wind down.
        os._exit(0)
    return True


def _relaunch_posix(target: str, install: pathlib.Path) -> None:
    """Restart into the freshly-installed binary."""
    if target == "cli":
        # Supervisor-agnostic in-place re-exec; preserves argv and PID lifecycle.
        logger.info("Re-executing %s", install)
        os.execv(str(install), [str(install), *sys.argv[1:]])
        return  # unreachable
    # Tray: never execv from inside the pystray loop — spawn detached and exit.
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-n", str(install)], close_fds=True)  # noqa: S603 S607
    else:
        subprocess.Popen([str(install)], start_new_session=True, close_fds=True)  # noqa: S603
    logger.info("Relaunched %s; exiting old process", install)
    os._exit(0)


def _rm(path: pathlib.Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            # Already absent or not removable; safe to ignore for a cleanup helper.
            pass


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def self_update(version: str, channel: str | None, *, restart: bool = True) -> bool:
    """Run one full self-update cycle. Returns True if an update was installed.

    No-op (returns False) when not a frozen build, inside Docker, already current,
    or another update is in progress.
    """
    if not _is_frozen():
        logger.info("Self-update skipped: not a frozen build (update via git/source)")
        return False
    if _is_docker():
        logger.info("Self-update skipped: running in Docker (repull the image instead)")
        return False

    with _single_flight() as acquired:
        if not acquired:
            return False

        target = _detect_target()
        eff_channel = (channel or parse_channel(version)[0] or "stable").lower()

        # Reuse the detection layer for the "is there anything newer?" decision.
        try:
            available = check_once(version, eff_channel)
        except Exception:
            logger.debug("Self-update version check failed", exc_info=True)
            return False
        if not available:
            logger.info("Self-update: already up to date (%s, %s)", version, eff_channel)
            return False

        try:
            release = _get_release_json(eff_channel)
            latest_ver = str(release.get("tag_name", "")).lstrip("v").strip()
            asset_name = resolve_asset_name(target, eff_channel, latest_ver)
            asset_url, sha_url = find_asset_urls(release, asset_name)
        except SelfUpdateUnsupportedError as exc:
            logger.warning("Self-update unsupported: %s", exc)
            return False
        except (SelfUpdateError, error.URLError, OSError, ValueError):
            logger.exception("Self-update: could not resolve release asset")
            return False

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="runway-update-"))
        try:
            archive = tmp / asset_name
            logger.info("Self-update: downloading %s", asset_name)
            _download(asset_url, archive)
            expected = _fetch_expected_sha(sha_url)
            if not verify_sha256(archive, expected):
                logger.error("Self-update aborted: checksum mismatch for %s", asset_name)
                return False
            extracted = tmp / "extracted"
            extracted.mkdir()
            _extract(archive, extracted)
            return apply_update(target, extracted, restart=restart)
        except (
            error.URLError,
            OSError,
            SelfUpdateError,
            tarfile.TarError,
            zipfile.BadZipFile,
        ):
            logger.exception("Self-update: download/extract failed")
            return False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
