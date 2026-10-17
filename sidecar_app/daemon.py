"""TrayDaemon — wraps DaemonRunner with a tray-friendly interface."""

import threading
import time
from collections.abc import Callable

from scripts import sidecar as _sidecar

DaemonRunner = _sidecar.DaemonRunner


class TrayDaemon:
    """Wraps DaemonRunner with a tray-friendly interface."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._runner: DaemonRunner = DaemonRunner(config, on_status_change=self._on_runner_status)
        self._lock = threading.Lock()
        self._last_stats: dict = {}
        self.on_status_change: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background DaemonRunner."""
        self._runner.start()

    def stop(self) -> None:
        """Stop the background DaemonRunner."""
        self._runner.stop()

    def pause(self) -> None:
        """Pause collection cycles."""
        self._runner.pause()

    def resume(self) -> None:
        """Resume collection cycles."""
        self._runner.resume()

    def trigger_now(self) -> None:
        """Run one collection cycle immediately in a new daemon thread."""
        t = threading.Thread(target=self._runner.run_once, name="TriggerNow", daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        """Delegate to DaemonRunner.status."""
        return self._runner.status

    @property
    def stats_summary(self) -> str:
        """Human-readable summary for the tray tooltip."""
        runner = self._runner
        parts: list[str] = []

        with runner._lock:
            last_sync = runner.last_sync_at
            last_upload = runner.last_upload_at
            metrics = runner.last_metrics_count
            events = runner.last_events_count

        now = time.time()
        for label, last_at in (("Last sync", last_sync), ("Last upload", last_upload)):
            if last_at is None:
                parts.append(f"{label}: never")
            else:
                age = max(0, int(now - last_at))
                elapsed = f"{age}s" if age < 60 else f"{age // 60}m"
                parts.append(f"{label}: {elapsed} ago")

        if last_upload is not None:
            parts.append(
                f"{metrics} metric{'s' if metrics != 1 else ''}, "
                f"{events} event{'s' if events != 1 else ''}"
            )

        err = runner.last_error
        if err:
            # Truncate long errors
            parts.append(f"Error: {err[:60]}")

        return " | ".join(parts)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_runner_status(self, status: str) -> None:
        """Invoked by DaemonRunner on each status change; forwards to tray."""
        with self._lock:
            self._last_stats = {
                "status": status,
                "last_cycle_at": self._runner.last_cycle_at,
                "last_metrics_count": self._runner.last_metrics_count,
                "last_events_count": self._runner.last_events_count,
                "last_upload_at": self._runner.last_upload_at,
                "last_sync_at": self._runner.last_sync_at,
            }
        if self.on_status_change is not None:
            self.on_status_change(status)
