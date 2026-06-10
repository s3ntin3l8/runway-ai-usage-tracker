"""GitHub Releases update checker for the Runway sidecar desktop app.

Notify-only: surfaces "(update available)" in the tray; the user downloads and
replaces the app manually. The actual stable/edge decision is delegated to
``scripts.sidecar_pkg.update_check`` so the tray and the headless CLI agree.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

from scripts.sidecar_pkg.update_check import check_once
from sidecar_app import __version__

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 86400  # 24h


def _resolve_channel() -> str | None:
    """Active update channel: env override > server-synced > inferred from version.

    The synced value is the channel the dashboard pushed to the running daemon
    (stored on ``scripts.sidecar._UPDATE_CHANNEL``); None lets ``check_once``
    infer it from this build's own version string.
    """
    env = os.environ.get("RUNWAY_UPDATE_CHANNEL")
    if env:
        return env
    try:
        import scripts.sidecar as _sidecar

        return _sidecar._UPDATE_CHANNEL
    except Exception:
        return None


class UpdateChecker:
    """Background thread that polls GitHub Releases once on start + every 24h."""

    def __init__(
        self,
        on_update_available: Callable[[str, str], None],  # (current_version, latest_or_description)
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
        try:
            available = check_once(__version__, _resolve_channel())
        except Exception:
            logger.debug("Update check failed", exc_info=True)
            return
        if available:
            self._on_update_available(__version__, available)

    def _loop(self) -> None:
        """Background thread loop: check on start, then every interval."""
        self.check_now()
        while not self._stop_event.wait(self._interval):
            self.check_now()
