"""The installed Antigravity CLI's version.

Google's Cloud Code Assist API decides *which quota pools* it serves on the
User-Agent's version, not only whether it answers at all: a stale version is
answered with 200 and a subset of the pools, so the dashboard silently loses a
quota bucket (issue #687). The CLI is installed on the sidecar's host and the
server never sees it, so the sidecar reports the version next to the OAuth
token and the collector builds its User-Agent from the report.

Reading the version costs a subprocess call, so the answer is cached: it only
changes when agy itself is updated.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.core.version import normalize_version

logger = logging.getLogger(__name__)

# The CLI only changes when agy itself is updated, so re-read it rarely.
_CACHE_TTL_SECONDS = 3600
# A *failed* read is not worth an hour: agy may not be on PATH yet at boot, or
# one read may time out. Retry soon instead of suppressing the report — and
# with it a quota pool — for the whole TTL.
_FAILURE_CACHE_TTL_SECONDS = 60
_COMMAND_TIMEOUT_SECONDS = 10


class _VersionCache:
    """The cached version and the lock that guards it.

    A plain module-level global would have to be rebound with ``global``,
    which reads badly and trips CodeQL's unused-global check.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: tuple[float, str | None] | None = None


_cache = _VersionCache()


def _creationflags() -> int:
    """Hide console windows opened by the child command on Windows."""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def resolve_agy() -> str | None:
    """Locate the agy binary: PATH first, then the common per-user installs."""
    found = shutil.which("agy")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "agy",
        home / "bin" / "agy",
        home / ".agy" / "bin" / "agy",
    ]
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        # Where the Windows installer puts the CLI.
        candidates.append(Path(local_appdata) / "agy" / "bin" / "agy.exe")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def parse_version(output: str | None) -> str | None:
    """The dotted version in *output*, or None when it is not one."""
    return normalize_version(output)


def _read_version(binary: str) -> str | None:
    """Run ``agy --version`` and parse it; None when the CLI cannot answer."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            # The locale codec would raise UnicodeDecodeError (a ValueError) on
            # bytes it cannot decode, which would unwind through
            # collect_provider and drop that provider's cards for the cycle.
            # The replacement characters are rejected by the validator anyway.
            errors="replace",
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
            creationflags=_creationflags(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.debug("agy --version could not be run", exc_info=True)
        return None
    if result.returncode != 0:
        logger.debug("agy --version exited with %s", result.returncode)
        return None
    version = normalize_version(result.stdout)
    if version is None:
        # A version shape this parser does not know (e.g. a future four-part
        # release). The server falls back to its floor version, so the report
        # is merely less precise — log it, or the next investigation starts
        # blind.
        logger.debug("agy --version printed an unrecognised version: %r", result.stdout[:80])
    return version


def installed_version(*, now: float | None = None) -> str | None:
    """The installed agy CLI's version, cached; None when unknown.

    A successful read is cached for an hour — the CLI only changes when agy
    itself is updated. A *failed* read is cached for a minute only: agy may
    not be on PATH yet at boot, or one read may time out, and caching that
    miss for an hour would suppress the report (and with it a quota pool)
    for the same span the issue this fixes took to notice.
    """
    moment = time.time() if now is None else now
    # The sidecar collects from a background thread while the tray may trigger a
    # refresh: the lock keeps a cold cache from spawning the CLI twice.
    with _cache.lock:
        if _cache.value is not None:
            cached_at, version = _cache.value
            ttl = _CACHE_TTL_SECONDS if version else _FAILURE_CACHE_TTL_SECONDS
            if moment - cached_at < ttl:
                return version
        binary = resolve_agy()
        version = _read_version(binary) if binary else None
        _cache.value = (moment, version)
        return version
