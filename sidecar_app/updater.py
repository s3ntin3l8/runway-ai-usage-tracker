"""GitHub Releases update checker for the Runway sidecar desktop app."""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from collections.abc import Callable

from sidecar_app import __version__

logger = logging.getLogger(__name__)

_RELEASES_URL = "https://github.com/s3ntin3l8/runway/releases"
_API_URL = "https://api.github.com/repos/s3ntin3l8/runway/releases/latest"
_CHECK_INTERVAL_SECONDS = 86400  # 24h


def _get_latest_version() -> str | None:
    """Fetch latest release tag from GitHub API. Returns None on any failure."""
    try:
        req = urllib.request.Request(  # noqa: S310
            _API_URL,
            headers={"User-Agent": f"Runway-Sidecar-Updater/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read().decode())
        return data["tag_name"].lstrip("v")
    except Exception:
        logger.debug("Update check failed", exc_info=True)
        return None


def _is_newer(latest: str, current: str) -> bool:
    """Return True if latest version is newer than current."""
    try:
        from packaging.version import Version

        return Version(latest) > Version(current)
    except Exception:
        return latest != current  # fallback


class UpdateChecker:
    """Background thread that polls GitHub Releases once on start + every 24h."""

    def __init__(
        self,
        on_update_available: Callable[[str, str], None],  # (current_version, latest_version)
        check_interval: int = _CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._on_update_available = on_update_available
        self._interval = check_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start background checking thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="runway-update-checker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal thread to stop."""
        self._stop_event.set()

    def check_now(self) -> None:
        """Run a single update check synchronously."""
        latest = _get_latest_version()
        if latest and _is_newer(latest, __version__):
            self._on_update_available(__version__, latest)

    def _loop(self) -> None:
        """Background thread loop: check on start, then every interval."""
        self.check_now()
        while not self._stop_event.wait(self._interval):
            self.check_now()
