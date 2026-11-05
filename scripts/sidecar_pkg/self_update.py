"""Self-update for the Runway sidecar: download → verify → swap → restart.

This is the *apply* layer that sits on top of the notify-only detection in
``scripts.sidecar_pkg.update_check``. It is shared by the headless CLI
(``scripts/sidecar.py``) and the desktop tray (``sidecar_app``), so — like
``update_check`` — it uses urllib plus the Sigstore verification extra and never imports
``app.*``; the frozen sidecar binary must stay self-contained.

Safety model:
  * **Frozen-only.** Self-update runs only under PyInstaller
    (``getattr(sys, "frozen", False)``). From-source checkouts and Docker
    containers are no-ops — they update via ``git pull`` / image repull.
  * **Sigstore mandatory.** Payloads must verify against the repository signing
    workflow and GitHub issuer before extraction; checksum-only releases fail.
  * **Checksum mandatory.** Every release asset ships a sibling ``.sha256``;
    we refuse to install bytes that don't verify.
  * **Download-then-swap.** The installed copy is never touched until the new
    asset is fully downloaded, verified, and extracted, so a failure leaves the
    running install intact.
  * **Single-flight.** An exclusive lock file prevents two updates at once.
  * **Single-slot rollback.** The replaced binary/bundle is kept as
    ``<install>.previous`` with its version in ``<install>.previous.version``;
    ``rollback()`` (tray "Roll back to …", CLI ``--rollback``) swaps it back.

Fetching a payload lives in ``self_update_fetch``, the Windows helper script in
``self_update_windows``, and the systemd-vs-exec choice in ``self_update_supervisor``.
Applying the swap stays here: that is the layer tests patch.

The OS-mutating ``apply_update`` is a thin per-platform shell, mostly verified
manually during release QA; the pure parts (asset-name resolution, checksum,
frozen/target detection) are unit tested, plus a targeted test for the macOS
.app exec-bit swap (see tests/unit/test_self_update.py) since a regression
there bricks the launch entirely rather than merely failing an update.
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
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib import (
    error,
    request,  # noqa: F401 — tests patch self_update.request.urlopen
)

from scripts.sidecar_pkg import asset_names, runtime_cleanup
from scripts.sidecar_pkg.keychain_client import close_keychain_client, reopen_keychain_client
from scripts.sidecar_pkg.signatures import UpdateVerificationError, verify_update
from scripts.sidecar_pkg.update_check import (
    _BETA_RELEASES_API_URL,
    _LATEST_URL,
    check_once,
    latest_beta_release,
    parse_channel,
)
from scripts.sidecar_pkg.update_limits import (
    MAX_ARCHIVE_BYTES,
    MAX_BUNDLE_BYTES,
    UpdateSizeError,
)

logger = logging.getLogger(__name__)

from scripts.sidecar_pkg.self_update_fetch import (
    _MAX_ATTEMPTS,
    SelfUpdateError,
    SelfUpdateUnsupportedError,
    _download,
    _extract,
    _fetch_expected_sha,
    _get_json,
    _github_ssl_context,
    _with_retries,
    verify_sha256,
)
from scripts.sidecar_pkg.self_update_supervisor import (
    _SYSTEMCTL_TIMEOUT_S,
    _fresh_runtime_env,
    _systemd_restart_exit_code,
)
from scripts.sidecar_pkg.self_update_windows import _windows_swap_script

# The security probe and the unit tests read these off this module.
__all__ = [
    "MAX_ARCHIVE_BYTES",
    "SelfUpdateError",
    "SelfUpdateUnsupportedError",
    "_MAX_ATTEMPTS",
    "_SYSTEMCTL_TIMEOUT_S",
    "_github_ssl_context",
    "apply_update",
    "find_asset_urls",
    "reclaim_stale_state",
    "register_pre_exec_hook",
    "resolve_asset_name",
    "rollback",
    "rollback_available",
    "running_from_disk_image",
    "self_update",
    "self_update_supported",
    "verify_sha256",
    "verify_update",
]

# The `edge` rolling prerelease object (carries assets[]); distinct from
# update_check._EDGE_REFS_URL, which only returns the git ref sha.
_EDGE_RELEASE_URL = (
    "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/releases/tags/edge"
)
_LOCK_NAME = "self-update.lock"


# ---------------------------------------------------------------------------
# Environment detection (pure)
# ---------------------------------------------------------------------------


def _is_frozen() -> bool:
    """True when running as a PyInstaller frozen binary."""
    return bool(getattr(sys, "frozen", False))


def _is_docker() -> bool:
    """Best-effort Docker detection — containers update by repulling the image."""
    return pathlib.Path("/.dockerenv").exists()


def running_from_disk_image(executable: str | None = None, plat: str | None = None) -> bool:
    """True when the macOS app is running straight off the mounted ``.dmg``.

    That happens when someone double-clicks the app inside the DMG window
    instead of dragging it to Applications first — or when Gatekeeper App
    Translocation runs a quarantined copy from a randomised read-only path.
    Either way the bundle is read-only and will vanish (unmount / reboot), so
    self-update and the login item must not target it.
    """
    if (plat or sys.platform) != "darwin":
        return False
    path = executable if executable is not None else sys.executable
    return path.startswith("/Volumes/") or "/AppTranslocation/" in path


def self_update_supported() -> bool:
    """True when this build can replace its own binary in place.

    Frozen PyInstaller builds only — Docker images update by repulling and
    from-source checkouts update via git, so both report False, as does a
    macOS app still running off its read-only disk image. Mirrors the gate in
    ``self_update()`` so the server is told the same thing the apply path
    enforces, and won't offer a meaningless update push.
    """
    return _is_frozen() and not _is_docker() and not running_from_disk_image()


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
    """Return the self-update payload asset name for this platform/target/channel.

    *version* is the latest release tag for the stable channel (``v`` prefix
    optional — the published names always carry it); the edge channel uses a
    single rolling asset per platform, so *version* is ignored there. Names
    come from ``asset_names`` — the same module the release-workflow contract
    test checks — and are always the ``.zip`` / ``.tar.gz`` payload, never the
    ``.dmg`` / ``-setup.exe`` installers.

    Raises ``SelfUpdateUnsupportedError`` for combinations with no published asset.
    """
    plat = asset_names.platform_key(sys.platform, target)
    if plat is None:
        raise SelfUpdateUnsupportedError(f"unsupported platform: {sys.platform}")
    try:
        label = asset_names.release_label(channel, version)
    except ValueError as exc:
        raise SelfUpdateError("missing latest version for stable asset name") from exc
    return asset_names.payload_name(plat, label)


def _asset_name_candidates(target: str, channel: str, version: str | None) -> list[str]:
    """Primary payload name, then the legacy ``v``-less spelling as a fallback."""
    primary = resolve_asset_name(target, channel, version)
    plat = asset_names.platform_key(sys.platform, target)
    legacy = (
        asset_names.legacy_payload_name(plat, asset_names.release_label(channel, version))
        if plat
        else None
    )
    return [primary] + ([legacy] if legacy and legacy != primary else [])


# ---------------------------------------------------------------------------
# GitHub release JSON + asset URLs
# ---------------------------------------------------------------------------


def _get_release_json(channel: str) -> dict:
    """Fetch the release object (with ``assets[]``) for the active channel."""
    if channel == "edge":
        return _get_json(_EDGE_RELEASE_URL)
    if channel == "beta":
        release = latest_beta_release(_get_json(_BETA_RELEASES_API_URL))
        if release is None:
            raise SelfUpdateError("no beta release is available")
        return release
    return _get_json(_LATEST_URL)


def find_asset_urls(release: dict, asset_name: str | list[str]) -> tuple[str, str]:
    """Return ``(asset_url, sha256_url)`` for *asset_name* within *release*.

    *asset_name* may be a list of candidate names tried in order (primary name
    first, legacy spelling after). Raises ``SelfUpdateError`` if no candidate is
    present, or the matched asset lacks its ``.sha256`` sibling — the checksum
    is mandatory.
    """
    by_name = {a.get("name"): a.get("browser_download_url") for a in release.get("assets", [])}
    candidates = [asset_name] if isinstance(asset_name, str) else list(asset_name)
    asset_name = next((n for n in candidates if by_name.get(n)), candidates[0])
    asset_url = by_name.get(asset_name)
    sha_url = by_name.get(f"{asset_name}.sha256")
    if not asset_url:
        raise SelfUpdateError(f"release asset not found: {asset_name}")
    if not sha_url:
        raise SelfUpdateError(f"checksum asset not found: {asset_name}.sha256")
    return asset_url, sha_url


# ---------------------------------------------------------------------------
# Single-flight lock
# ---------------------------------------------------------------------------


# An update applies + restarts in seconds; a lock older than this was orphaned
# by a crash (or a pre-fix build that re-exec'd without releasing it — see
# _release_lock) and is safe to reclaim.
_LOCK_STALE_SECONDS = 900  # 15 min


def _lock_path() -> pathlib.Path:
    return _sidecar_dir() / _LOCK_NAME


# Callbacks run right before ``os.execve`` replaces the process image. ``execve`` keeps the
# PID, and nothing (atexit, finally) runs across it, so anything that must not outlive the
# old image — the sidecar's PID file — has to be released here, or the re-exec'd daemon sees
# its own PID in the file, reports "already running" and exits.
_PRE_EXEC_HOOKS: list[tuple[Callable[[], None], Callable[[], None] | None]] = []


def register_pre_exec_hook(
    hook: Callable[[], None], on_failure: Callable[[], None] | None = None
) -> None:
    """Run ``hook`` just before a CLI self-update re-execs (best-effort, idempotent).

    ``on_failure`` undoes it if ``execve`` itself fails: the old image is then still running
    and must not be left without whatever ``hook`` released (e.g. its PID file).
    """
    if all(existing != hook for existing, _ in _PRE_EXEC_HOOKS):
        _PRE_EXEC_HOOKS.append((hook, on_failure))


def _call_best_effort(fn: Callable[[], None], what: str) -> None:
    try:
        fn()
    except Exception:
        # A failing cleanup must not abort the update that is about to relaunch.
        logger.warning("%s %s failed", what, getattr(fn, "__name__", fn), exc_info=True)


def _run_pre_exec_hooks() -> None:
    for hook, _undo in list(_PRE_EXEC_HOOKS):
        _call_best_effort(hook, "Pre-exec hook")


def _undo_pre_exec_hooks() -> None:
    for _hook, undo in list(_PRE_EXEC_HOOKS):
        if undo is not None:
            _call_best_effort(undo, "Pre-exec undo")


# Only _single_flight registers ownership; release/context teardown retire it.
_ACTIVE_LOCK_LEASE: tuple[int, pathlib.Path] | None = None
# Protects in-process lease ownership and retirement; O_EXCL excludes other processes.
_ACTIVE_LOCK_GUARD = threading.Lock()


def _release_owned_lock(lease: tuple[int, pathlib.Path]) -> None:
    global _ACTIVE_LOCK_LEASE
    with _ACTIVE_LOCK_GUARD:
        if _ACTIVE_LOCK_LEASE is not lease:
            return
        _ACTIVE_LOCK_LEASE = None
        fd, lock_path = lease
        # Windows cannot unlink this lock while our CRT descriptor is open.
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            # Already removed, or left for stale-lock reclaim after a transient
            # filesystem/antivirus refusal. The owned CRT handle is closed.
            pass


def _release_lock() -> None:
    """Remove the single-flight lock file.

    A successful update ends in ``os.execve`` / ``os._exit`` (see the relaunch
    helpers), which replace the process image and so never run the
    ``_single_flight`` context manager's ``finally``. Without an explicit
    release here every successful update would orphan the lock and wedge all
    future updates with "already in progress". Call this immediately before any
    re-exec/exit.
    """
    with _ACTIVE_LOCK_GUARD:
        lease = _ACTIVE_LOCK_LEASE
    if lease is not None:
        _release_owned_lock(lease)


@contextmanager
def _single_flight() -> Iterator[bool]:
    """Yield True if we acquired the update lock, False if one is already held.

    Uses ``O_CREAT|O_EXCL`` (same TOCTOU-free pattern as the CLI PID file). A
    lock older than ``_LOCK_STALE_SECONDS`` is treated as orphaned and reclaimed
    so a leaked lock self-heals instead of wedging updates forever.
    """
    global _ACTIVE_LOCK_LEASE
    lock_dir = _sidecar_dir()
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # If we can't even create the dir, treat as un-lockable but proceed.
        yield True
        return
    lock_path = lock_dir / _LOCK_NAME
    fd: int | None = None
    lease: tuple[int, pathlib.Path] | None = None
    try:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not _lock_is_stale(lock_path):
                logger.warning("Self-update already in progress; skipping")
                yield False
                return
            logger.warning(
                "Reclaiming stale self-update lock (older than %ds)", _LOCK_STALE_SECONDS
            )
            try:
                lock_path.unlink()
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except (OSError, FileExistsError):
                logger.warning("Self-update already in progress; skipping")
                yield False
                return
        lease = (fd, lock_path)
        with _ACTIVE_LOCK_GUARD:
            _ACTIVE_LOCK_LEASE = lease
        yield True
    finally:
        if lease is not None:
            # Identity prevents a released/recycled descriptor from closing a
            # newer updater's lock when this older context exits.
            _release_owned_lock(lease)


def _lock_is_stale(lock_path: pathlib.Path) -> bool:
    """True when the lock file predates ``_LOCK_STALE_SECONDS`` (orphaned)."""
    try:
        return (time.time() - lock_path.stat().st_mtime) > _LOCK_STALE_SECONDS
    except OSError:
        return False


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


# Windows installer's Apps & Features entry (installer/windows/runway-sidecar.nsi).
# Self-updates refresh its DisplayVersion; portable installs have no such key.
_WIN_UNINSTALL_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Runway Sidecar"
_PREVIOUS_SUFFIX = ".previous"
_VERSION_SUFFIX = ".version"


def _previous_path(install: pathlib.Path) -> pathlib.Path:
    return install.with_name(install.name + _PREVIOUS_SUFFIX)


def _previous_version_path(install: pathlib.Path) -> pathlib.Path:
    return install.with_name(install.name + _PREVIOUS_SUFFIX + _VERSION_SUFFIX)


def _write_previous_version(install: pathlib.Path, version: str | None) -> None:
    """Record which version the ``.previous`` backup is (for the rollback label)."""
    try:
        _previous_version_path(install).write_text((version or "").strip() + "\n")
    except OSError:
        logger.debug("Could not write previous-version marker", exc_info=True)


def _windows_transaction_path(install: pathlib.Path) -> pathlib.Path:
    return install.with_name(install.name + ".previous.transaction.json")


def _file_digest(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _windows_transaction(install: pathlib.Path) -> dict[str, Any]:
    record = json.loads(_windows_transaction_path(install).read_text())
    if not isinstance(record, dict):
        raise ValueError("Invalid Windows update transaction")
    for name in ("original", "incoming", "previous"):
        item = record[name]
        if not isinstance(item, dict) or not isinstance(item["version"], str):
            raise ValueError("Invalid Windows update transaction entry")
        digest = item["sha256"]
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("Invalid Windows update transaction digest")
    helper_pid = record["helper_pid"]
    if helper_pid is not None and (not isinstance(helper_pid, int) or helper_pid <= 0):
        raise ValueError("Invalid Windows update helper PID")
    return record


def _write_windows_transaction(install: pathlib.Path, record: dict[str, Any]) -> None:
    path = _windows_transaction_path(install)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    os.replace(temporary, path)


def rollback_available() -> str | None:
    """Version string of the kept backup when a rollback is possible, else ``None``.

    ``""`` means a backup exists but its version is unknown.
    """
    if not self_update_supported():
        return None
    install = _install_path()
    if not _previous_path(install).exists():
        return None
    if _windows_transaction_path(install).exists():
        try:
            record = _windows_transaction(install)
            digest = _file_digest(_previous_path(install))
            for name in ("original", "previous"):
                if digest is not None and digest == record[name]["sha256"]:
                    return str(record[name]["version"])
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Cannot identify interrupted Windows rollback backup", exc_info=True)
        return ""  # An unmatched backup must not inherit another executable's label.
    try:
        return _previous_version_path(install).read_text().strip()
    except OSError:
        return ""


def apply_update(
    target: str,
    staged_dir: pathlib.Path,
    *,
    restart: bool,
    current_version: str | None = None,
    new_version: str | None = None,
    cleanup: Callable[[], None] | None = None,
) -> bool:
    """Swap the running install with the staged copy and optionally relaunch.

    The replaced copy is kept as ``<install>.previous`` (tagged with
    *current_version*) for ``rollback()``. *new_version* refreshes the Windows
    installer's Apps & Features entry. Returns True on a successful swap. On a
    non-writable install path it logs and returns False without leaving
    partial state. *cleanup* (POSIX only) runs iff a relaunch actually replaces the
    running image, directly via ``execve`` or indirectly via ``os._exit`` after
    spawning the new one, i.e. exactly when the caller's ``finally`` is skipped.
    """
    install = _install_path()
    staged = _find_staged(staged_dir, install)

    if sys.platform == "win32":
        if _windows_transaction_path(install).exists():
            logger.error("Self-update requires recovery of the previous Windows transaction")
            return False
        new_exe = install.with_name(install.stem + ".new" + install.suffix)
        try:
            if new_exe.exists():
                _rm(new_exe)
            shutil.move(str(staged), str(new_exe))
        except OSError:
            logger.exception("Self-update staging failed")
            return False
        return _apply_windows(
            install,
            new_exe,
            restart=restart,
            display_version=new_version,
            backup_version=current_version,
        )

    return _swap_posix(
        target,
        install,
        staged,
        restart=restart,
        backup_version=current_version,
        cleanup=cleanup,
    )


def _swap_posix(
    target: str,
    install: pathlib.Path,
    incoming: pathlib.Path,
    *,
    restart: bool,
    backup_version: str | None,
    cleanup: Callable[[], None] | None = None,
) -> bool:
    """POSIX (Linux + macOS) swap: move the running file/bundle to ``.previous``
    and *incoming* into place. A running file's open inode survives the move."""
    previous = _previous_path(install)
    try:
        if previous.exists():
            _rm(previous)
        os.rename(install, previous)
        shutil.move(str(incoming), str(install))
        if install.is_file():
            # Owner-only rwx: the binary is re-exec'd as the user that runs it,
            # so it needs no group/world bits.
            os.chmod(install, 0o700)
        elif sys.platform == "darwin" and install.suffix == ".app":
            # `install` is a bundle directory, not a file, so the branch above
            # never fires — this used to leave Contents/MacOS/* without an
            # exec bit whenever the archive's own permissions were lost (see
            # _extract). Chmod every entry-point directly (recursively, since
            # some bundles nest helper executables under Contents/MacOS/) as
            # a safety net that doesn't depend on archive metadata surviving
            # extraction. Owner-only rwx, same policy as the file branch above.
            macos_dir = install / "Contents" / "MacOS"
            if macos_dir.is_dir():
                for entry in macos_dir.rglob("*"):
                    if entry.is_file():
                        os.chmod(entry, 0o700)
    except PermissionError:
        logger.error(
            "Self-update aborted: install path %s is not writable; update manually", install
        )
        # Best-effort restore if we moved the original aside.
        if not install.exists() and previous.exists():
            try:
                os.rename(previous, install)
            except OSError:
                # Restore is best-effort; nothing more we can do if it also fails.
                pass
        return False
    except OSError:
        logger.exception("Self-update swap failed")
        return False

    _write_previous_version(install, backup_version)
    # Pre-rollback builds kept a `.old` breadcrumb; it is superseded now.
    _rm(install.with_name(install.name + ".old"))
    logger.info("Installed %s (previous kept at %s)", install, previous)
    if restart:
        _relaunch_posix(target, install, cleanup=cleanup)
    return True


def _apply_windows(
    install: pathlib.Path,
    incoming: pathlib.Path,
    *,
    restart: bool,
    display_version: str | None = None,
    backup_version: str | None = None,
) -> bool:
    """Windows swap: a running .exe can't be overwritten, so a detached helper
    waits for us to exit, swaps *incoming* in (keeping the old exe as
    ``.previous``), and relaunches."""
    if _windows_transaction_path(install).exists():
        logger.error("Windows update transaction requires recovery before helper launch")
        return False
    helper = install.with_name("runway-self-update.bat")
    pending_version = install.with_name(install.name + ".previous.pending.version")
    script = _windows_swap_script(
        os.getpid(),
        install,
        incoming,
        _previous_path(install),
        restart=restart,
        display_version=display_version,
    )
    try:
        # Record identities before any detached moves. Readers can label a
        # backup safely even if the helper is interrupted before metadata commit.
        try:
            previous_version = _previous_version_path(install).read_text().strip()
        except FileNotFoundError:
            previous_version = ""
        record: dict[str, Any] = {
            "original": {"sha256": _file_digest(install), "version": backup_version or ""},
            "incoming": {"sha256": _file_digest(incoming), "version": display_version or ""},
            "previous": {
                "sha256": _file_digest(_previous_path(install)),
                "version": previous_version,
            },
            "helper_pid": None,  # Unknown until detached-helper ownership is persisted.
        }
        # Rollback already moved its backup into the incoming slot.
        if incoming == install.with_name(install.name + ".rollback"):
            record["previous"] = {"sha256": _file_digest(incoming), "version": previous_version}
        _write_windows_transaction(install, record)
        pending_version.write_text((backup_version or "").strip() + "\n")
        helper.write_text(script)
    except OSError:
        logger.exception("Self-update helper write failed")
        for path in (helper, pending_version, _windows_transaction_path(install)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Could not remove failed Windows staging file %s", path, exc_info=True
                )
        return False

    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    # CreateProcess's explicit application name is not resolved through PATH.
    # Use the OS command processor, even when the install/CWD is elsewhere.
    command_processor = (
        pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
    )
    try:
        # cmd /s removes the outer quotes and keeps the quoted batch path.
        # Passing the path as a separate argv element can lose its quotes when
        # the per-user install directory contains spaces.
        process = subprocess.Popen(  # noqa: S603
            f'"{command_processor}" /d /s /c ""{helper}""',
            executable=str(command_processor),
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError:
        logger.exception("Could not start Windows self-update helper %s", helper)
        staged_files = [helper, pending_version, _windows_transaction_path(install)]
        if incoming != install.with_name(install.name + ".rollback"):
            staged_files.append(incoming)
        for staged_file in staged_files:
            try:
                staged_file.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Could not remove failed Windows self-update file %s",
                    staged_file,
                    exc_info=True,
                )
        return False
    if process is not None:
        record["helper_pid"] = process.pid
        try:
            _write_windows_transaction(install, record)
        except OSError:
            # The helper waits for this process; keep it from swapping after a
            # failed ownership-record write, and retain metadata for recovery.
            try:
                process.terminate()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                logger.warning(
                    "Could not stop the Windows helper; retaining recovery state", exc_info=True
                )
            logger.exception("Could not record Windows helper ownership")
            return False
    logger.info("Self-update staged; helper will swap %s after exit", install)
    if restart:
        # The helper relaunches once we exit; leave the loop / process to wind down.
        # Release the lock first — os._exit bypasses the context manager cleanup.
        _release_lock()
        # os._exit skips atexit handlers, so the single-instance lock has to be
        # released here too — the same reason the POSIX path runs the pre-exec
        # hooks. Otherwise the relaunched sidecar reads the outgoing (dead) PID
        # from the lock file, reports "already running" and exits.
        _run_pre_exec_hooks()
        close_keychain_client()
        os._exit(0)
    return True


def rollback(current_version: str, *, restart: bool = True) -> bool:
    """Swap the kept ``.previous`` build back in (and keep the current one as
    the new ``.previous``, so a rollback can itself be undone).

    Returns False when there is nothing to roll back to or the swap fails.
    """
    if not self_update_supported():
        logger.info("Rollback skipped: not a self-updatable install")
        return False
    with _single_flight() as acquired:
        if not acquired:
            return False
        install = _install_path()
        previous = _previous_path(install)
        if sys.platform == "win32" and _windows_transaction_path(install).exists():
            logger.error("Rollback requires recovery of the previous Windows transaction")
            return False
        if not previous.exists():
            logger.info("Rollback skipped: no previous build kept next to %s", install)
            return False
        try:
            target_version = _previous_version_path(install).read_text().strip() or None
        except OSError:
            target_version = None

        # Move the backup out of the `.previous` slot first so the swap can
        # refill that slot with the build we're rolling back from.
        incoming = install.with_name(install.name + ".rollback")
        try:
            if incoming.exists():
                _rm(incoming)
            os.rename(previous, incoming)
        except OSError:
            logger.exception("Rollback staging failed")
            return False

        logger.info("Rolling back %s to %s", install, target_version or "previous build")
        if sys.platform == "win32":
            ok = _apply_windows(
                install,
                incoming,
                restart=restart,
                display_version=target_version,
                backup_version=current_version,
            )
            if not ok and not previous.exists() and incoming.exists():
                try:
                    os.rename(incoming, previous)
                except OSError:
                    logger.warning("Rollback backup remains at %s", incoming, exc_info=True)
            return ok
        ok = _swap_posix(
            _detect_target(), install, incoming, restart=restart, backup_version=current_version
        )
        if not ok and not previous.exists() and incoming.exists():
            try:
                os.rename(incoming, previous)  # leave the backup where we found it
            except OSError:
                # Best-effort: the failed swap is already logged; the backup
                # stays at `<install>.rollback` for manual recovery.
                pass
        return ok


def _relaunch_posix(
    target: str, install: pathlib.Path, *, cleanup: Callable[[], None] | None = None
) -> None:
    """Restart into the freshly-installed binary."""
    # Release the single-flight lock before any process replacement — os.execve /
    # os._exit never run the _single_flight finally, so the lock would leak and
    # wedge every future update.
    if target == "cli":
        exit_code = _systemd_restart_exit_code()
        _release_lock()
        _run_pre_exec_hooks()
        if cleanup is not None:
            _call_best_effort(cleanup, "Pre-relaunch cleanup")
        if exit_code is not None:
            # systemd runs us as bootloader -> python. Exiting lets the bootloader remove this
            # runtime dir, and systemd respawns a clean two-level tree. An in-place execve
            # would instead leave the old bootloader waiting on the new one, holding the old
            # ~100 MB runtime dir (and an extra process) until the next restart. A chain nested
            # by an earlier execve update unwinds the same way: each bootloader exits in turn.
            logger.info(
                "Update installed; exiting (%d) for systemd to restart %s", exit_code, install
            )
            logging.shutdown()
            close_keychain_client()
            os._exit(exit_code)
            return  # unreachable
        # No such supervisor: re-exec in place. The Python process gets a NEW pid (the
        # exec'd image is a fresh bootloader that spawns it), and the old parent bootloader
        # lingers idle until the next restart, so only its runtime dir is reclaimed.
        logger.info("Re-executing %s", install)
        try:
            close_keychain_client()
            os.execve(
                str(install),
                [str(install), *sys.argv[1:]],
                _fresh_runtime_env(hand_off_runtime=True),
            )
        except OSError:
            reopen_keychain_client()
            # execve failed (unreadable/ENOEXEC binary): this image keeps running, so hand back
            # what the hooks released before the caller logs the failed update.
            _undo_pre_exec_hooks()
            raise
        return  # unreachable
    # Tray: never exec from inside the pystray loop — spawn detached and exit. Deliberately no
    # RUNWAY_RETIRED_MEIPASS hand-off or systemd exit here: the old bootloader removes its own
    # runtime dir when this process exits, so nothing is left to reclaim.
    if cleanup is not None:
        _call_best_effort(cleanup, "Pre-relaunch cleanup")
    # `open` hands its caller's environment to the launched app, so scrub on macOS too.
    env = _fresh_runtime_env()
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-n", str(install)], close_fds=True, env=env)  # noqa: S603 S607
    else:
        subprocess.Popen(  # noqa: S603
            [str(install)], start_new_session=True, close_fds=True, env=env
        )
    logger.info("Relaunched %s; exiting old process", install)
    _release_lock()
    close_keychain_client()
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


# A rollback swaps in milliseconds; older than this means it was interrupted.
_ROLLBACK_MAX_AGE_S = 600


def _windows_helper_is_alive(pid: int) -> bool:
    """Unknown process status must block destructive recovery, like a live helper."""
    if os.name != "nt":
        return runtime_cleanup.pid_is_alive(pid)
    # The shared Windows probe: OpenProcess + WaitForSingleObject, so a process
    # that exited while a handle to it stayed open is correctly read as dead.
    return runtime_cleanup.windows_pid_is_alive(pid)


def _recover_windows_transaction(install: pathlib.Path, now: float | None) -> bool:
    """Recover only identified bytes after an old, positively inactive helper."""
    path = _windows_transaction_path(install)
    record = _windows_transaction(install)
    if path.stat().st_mtime > (time.time() if now is None else now) - _ROLLBACK_MAX_AGE_S:
        return False
    if record["helper_pid"] is None or _windows_helper_is_alive(record["helper_pid"]):
        return False
    previous = _previous_path(install)
    saved = previous.with_name(previous.name + "_old")
    pending = install.with_name(install.name + ".previous.pending.version")
    incoming = install.with_name(install.name + ".rollback")
    if not incoming.exists():
        incoming = install.with_name(install.stem + ".new" + install.suffix)
    original = record["original"]["sha256"]
    old_backup = record["previous"]["sha256"]
    replacement = record["incoming"]["sha256"]
    if original is None or replacement is None:
        return False
    installed_digest = _file_digest(install)
    previous_digest = _file_digest(previous)
    saved_digest = _file_digest(saved)
    if saved_digest is not None and saved_digest != old_backup:
        return False
    if installed_digest == replacement and previous_digest == original and not incoming.exists():
        # The incoming rename committed; repair its backup label even when the
        # helper died before moving pending metadata or cleaning the old slot.
        version = record["original"]["version"]
    else:
        # Undo an incomplete swap, but never overwrite unidentified bytes.
        if installed_digest is None and previous_digest == original:
            os.replace(previous, install)
            installed_digest = original
            previous_digest = None
        if installed_digest != original:
            return False
        if previous_digest is None and old_backup is not None:
            source = saved if saved_digest == old_backup else incoming
            if _file_digest(source) != old_backup:
                return False
            os.replace(source, previous)
            previous_digest = old_backup
        if previous_digest != old_backup:
            return False
        version = record["previous"]["version"]
    # A successful replace is required before recovery files are removed.
    if previous.exists():
        pending.write_text(version + "\n")
        os.replace(pending, _previous_version_path(install))
    else:
        _previous_version_path(install).unlink(missing_ok=True)
    for leftover in (saved, saved.with_name(saved.name + _VERSION_SUFFIX), pending):
        leftover.unlink(missing_ok=True)
    path.unlink()
    logger.info("Recovered interrupted Windows update metadata for %s", install)
    return True


def recover_stranded_rollback(now: float | None = None) -> int:
    """Undo a ``rollback()`` that was killed between its two renames.

    ``rollback()`` moves ``<install>.previous`` to ``<install>.rollback`` before the swap.
    A kill in between strands the backup there, and ``rollback_available()`` then reports
    nothing to roll back to. Put it back (or drop the duplicate); return bytes freed.
    """
    if not _is_frozen():
        return 0
    install = _install_path()
    if _windows_transaction_path(install).exists():
        _recover_windows_transaction(install, now)
        return 0  # Preserve ambiguous state instead of dropping a distinct backup.
    stranded = install.with_name(install.name + ".rollback")
    if not stranded.exists():
        return 0
    cutoff = (time.time() if now is None else now) - _ROLLBACK_MAX_AGE_S
    # ctime, not mtime: the rename updates it, mtime is that of the original backup.
    if stranded.lstat().st_ctime > cutoff:
        return 0
    previous = _previous_path(install)
    if not previous.exists():
        os.rename(stranded, previous)
        logger.info("Restored the rollback backup stranded at %s", stranded)
        return 0
    freed = runtime_cleanup.dir_size(stranded) if stranded.is_dir() else stranded.stat().st_size
    _rm(stranded)
    return freed


def reclaim_stale_state() -> None:
    """Startup hook: sweep dead builds' temp dirs, then finish an interrupted rollback."""
    runtime_cleanup.startup_cleanup()
    try:
        freed = recover_stranded_rollback()
    except Exception:
        logger.debug("Stranded-rollback recovery failed", exc_info=True)
        return
    if freed:
        logger.info("Reclaimed %.1f MB of a stale rollback backup", freed / 1_000_000)


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
    if running_from_disk_image():
        logger.info(
            "Self-update skipped: running from the disk image; move the app to Applications"
        )
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
            release = _with_retries(lambda: _get_release_json(eff_channel), what="release fetch")
            latest_tag = str(release.get("tag_name", "")).strip()
            candidates = _asset_name_candidates(target, eff_channel, latest_tag)
            published = {a.get("name") for a in release.get("assets", [])}
            asset_name = next((n for n in candidates if n in published), candidates[0])
            asset_url, sha_url = find_asset_urls(release, asset_name)
            bundle_url = next(
                (
                    a.get("browser_download_url")
                    for a in release.get("assets", [])
                    if a.get("name") == f"{asset_name}.sigstore.json"
                ),
                None,
            )
            if not bundle_url:
                raise SelfUpdateError("Release lacks mandatory Sigstore verification bundle")
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
            _with_retries(lambda: _download(asset_url, archive), what="asset download")
            expected = _with_retries(lambda: _fetch_expected_sha(sha_url), what="checksum fetch")
            if not verify_sha256(archive, expected):
                logger.error("Self-update aborted: checksum mismatch for %s", asset_name)
                return False
            bundle_path = tmp / f"{asset_name}.sigstore.json"
            _with_retries(
                lambda: _download(bundle_url, bundle_path, max_bytes=MAX_BUNDLE_BYTES),
                what="signature bundle download",
            )
            verify_update(archive, bundle_path)
            extracted = tmp / "extracted"
            extracted.mkdir()
            _extract(archive, extracted)
            return apply_update(
                target,
                extracted,
                restart=restart,
                current_version=version,
                new_version=latest_tag.lstrip("vV") or None,
                cleanup=lambda: shutil.rmtree(tmp, ignore_errors=True),
            )
        except (
            error.URLError,
            OSError,
            SelfUpdateError,
            UpdateVerificationError,
            UpdateSizeError,
            tarfile.TarError,
            zipfile.BadZipFile,
        ):
            logger.exception("Self-update: download/extract failed")
            return False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
