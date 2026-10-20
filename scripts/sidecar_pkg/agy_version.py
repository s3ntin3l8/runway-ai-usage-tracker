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
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ``agy --version`` prints a bare version ("1.4.3"). Only a dotted number is
# accepted: the value ends up in an HTTP header, so it must never carry
# arbitrary text.
_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+){0,2})\s*$")

# The CLI only changes when agy itself is updated, so re-read it rarely.
_CACHE_TTL_SECONDS = 3600
_COMMAND_TIMEOUT_SECONDS = 10

_cached_version: tuple[float, str | None] | None = None
_cache_lock = threading.Lock()


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
    if not output:
        return None
    match = _VERSION_RE.match(output)
    return match.group(1) if match else None


def _read_version(binary: str) -> str | None:
    """Run ``agy --version`` and parse it; None when the CLI cannot answer."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
            creationflags=_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("agy --version could not be run", exc_info=True)
        return None
    if result.returncode != 0:
        logger.debug("agy --version exited with %s", result.returncode)
        return None
    return parse_version(result.stdout)


def installed_version(*, now: float | None = None, refresh: bool = False) -> str | None:
    """The installed agy CLI's version, cached for an hour; None when unknown.

    ``None`` means "the sidecar could not tell" — the server then falls back to
    its own floor version. It never means "agy is old".
    """
    global _cached_version
    moment = time.time() if now is None else now
    # The sidecar collects from a background thread while the tray may trigger a
    # refresh: the lock keeps a cold cache from spawning the CLI twice.
    with _cache_lock:
        if not refresh and _cached_version is not None:
            cached_at, version = _cached_version
            if moment - cached_at < _CACHE_TTL_SECONDS:
                return version
        binary = resolve_agy()
        version = _read_version(binary) if binary else None
        _cached_version = (moment, version)
        return version
