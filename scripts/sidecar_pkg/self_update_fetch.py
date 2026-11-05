"""Download, verify, and extract a sidecar self-update payload.

Split out of ``self_update.py``. The apply path stays there and calls these
helpers through that module, so ``monkeypatch.setattr(self_update, "_get_json", ...)``
still replaces the release lookup ``_get_release_json`` performs.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tarfile
import time
import zipfile
from collections.abc import Callable
from typing import Any
from urllib import error, request

from scripts.sidecar_pkg.update_limits import (
    MAX_CHECKSUM_BYTES,
    MAX_METADATA_BYTES,
    UpdateSizeError,
    bounded_chunks,
    bounded_read,
)

logger = logging.getLogger("scripts.sidecar_pkg.self_update")


def _apply_module():
    """The ``self_update`` module that tests patch, already imported by the caller.

    Accept either import spelling. ``scripts/`` is on ``sys.path`` in some
    tests, which registers the module as ``sidecar_pkg.self_update`` rather
    than ``scripts.sidecar_pkg.self_update``.
    """
    import sys

    for name in ("scripts.sidecar_pkg.self_update", "sidecar_pkg.self_update"):
        module = sys.modules.get(name)
        if module is not None:
            return module
    raise RuntimeError(
        "self-update helpers must be called through self_update; "
        "it is not loaded under scripts.sidecar_pkg.self_update or sidecar_pkg.self_update"
    )


def _patched(name: str):
    """Read *name* from ``self_update`` at call time.

    Tests replace helpers and limits on that module (``_github_ssl_context``,
    ``MAX_ARCHIVE_BYTES``, ``hashlib``). A direct global here would keep the
    value this module was imported with.
    """
    return getattr(_apply_module(), name)


_TIMEOUT_SECONDS = 60

# Bounded retry for transient GitHub failures (5xx / rate-limit / timeouts). A
# single flaky response used to abort the whole update with no second attempt
# until the next manual push, so we soak up blips here. Other 4xx (e.g. 404 =
# asset genuinely absent) are NOT retried — they re-raise immediately.
_MAX_ATTEMPTS = 3  # 1 try + 2 retries
_BACKOFF_BASE_SECONDS = 2  # 2s, 4s → worst-case ~6s added latency
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _with_retries[T](fn: Callable[[], T], *, what: str) -> T:
    """Call *fn* with bounded exponential backoff on transient network errors.

    Retries on 5xx / 429 and connection/timeout errors; never on other 4xx
    (e.g. 404 = asset genuinely absent). Re-raises the last error once attempts
    are exhausted so the caller's existing ``except`` handling still applies.
    """
    last: BaseException | None = None
    attempts = _patched("_MAX_ATTEMPTS")
    for attempt in range(attempts):
        try:
            return fn()
        except error.HTTPError as exc:
            if exc.code not in _RETRYABLE_STATUS:
                raise
            last = exc
        except (error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < attempts - 1:
            wait = _BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "Self-update: %s failed (%s); retry %d/%d in %ds",
                what,
                last,
                attempt + 1,
                attempts - 1,
                wait,
            )
            time.sleep(wait)
    assert last is not None  # loop always sets last before exhausting
    raise last


class SelfUpdateError(Exception):
    """A recoverable self-update failure (network, missing asset, bad checksum)."""


class SelfUpdateUnsupportedError(SelfUpdateError):
    """Self-update is not available for this platform/channel combination."""


def _github_ssl_context(url: str):  # type: ignore[no-untyped-def]
    """certifi-backed TLS context so the frozen binary trusts GitHub's cert.

    GitHub always presents a valid public cert, so verification is never
    disabled here even if the user set ``RUNWAY_INSECURE`` for their own server.
    """
    from scripts.sidecar_pkg.tls import build_context

    return build_context(url, insecure=False)


def _get_json(url: str) -> Any:
    req = request.Request(  # noqa: S310 — fixed https GitHub API URL
        url, headers={"User-Agent": "Runway-Sidecar-SelfUpdate"}
    )
    with request.urlopen(
        req, timeout=_TIMEOUT_SECONDS, context=_patched("_github_ssl_context")(url)
    ) as resp:  # noqa: S310
        return json.loads(bounded_read(resp, MAX_METADATA_BYTES).decode())


# ---------------------------------------------------------------------------
# Download + verify
# ---------------------------------------------------------------------------


def _download(url: str, dest: pathlib.Path, *, max_bytes: int | None = None) -> None:
    if max_bytes is None:
        max_bytes = _patched("MAX_ARCHIVE_BYTES")
    req = request.Request(url, headers={"User-Agent": "Runway-Sidecar-SelfUpdate"})  # noqa: S310
    ctx = _patched("_github_ssl_context")(url)
    try:
        with (
            request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=ctx) as resp,
            open(dest, "wb") as fh,
        ):  # noqa: S310
            for chunk in bounded_chunks(resp, max_bytes):
                fh.write(chunk)
    except BaseException:
        # Interruptions and process-exit exceptions must also remove partial
        # resources; cleanup never converts the original failure into success.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove partial update resource", exc_info=True)
        raise


def _fetch_expected_sha(url: str) -> str:
    """Download a ``.sha256`` file and return the lowercased hex digest."""
    req = request.Request(url, headers={"User-Agent": "Runway-Sidecar-SelfUpdate"})  # noqa: S310
    with request.urlopen(
        req, timeout=_TIMEOUT_SECONDS, context=_patched("_github_ssl_context")(url)
    ) as resp:  # noqa: S310
        text = bounded_read(resp, MAX_CHECKSUM_BYTES).decode().strip()
    # shasum format: "<hex>  <filename>" — take the first token.
    return text.split()[0].lower() if text else ""


def verify_sha256(path: pathlib.Path, expected_hex: str) -> bool:
    """True when the SHA-256 of *path* matches *expected_hex* (case-insensitive)."""
    if path.stat().st_size > _patched("MAX_ARCHIVE_BYTES"):
        raise UpdateSizeError("Update archive exceeds size limit")
    if not expected_hex:
        return False
    h = _patched("hashlib").sha256()
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
            # zipfile.extractall drops Unix permission bits (it only writes
            # file *contents*), even though `zip -r` recorded the original
            # mode in each member's external_attr. Restore it so bundled
            # Mach-O/ELF executables (e.g. Contents/MacOS/RunwaySidecar in a
            # macOS .app) stay launchable after extraction. Directories are
            # intentionally left alone: extractall's own directory-creation
            # default (currently 0o755, traversable) is what keeps `.app`
            # dirs walkable, and the apply_update() safety net below covers
            # files regardless of what that default does.
            for zinfo in zf.infolist():
                mode = (zinfo.external_attr >> 16) & 0o777
                if mode:  # 0 on archives written without Unix attrs — no-op
                    member = dest / zinfo.filename
                    if member.is_file():
                        os.chmod(member, mode)
    else:
        with tarfile.open(archive) as tf:
            # PEP 706 "data" filter blocks absolute paths, "..", and unsafe links.
            tf.extractall(dest, filter="data")
