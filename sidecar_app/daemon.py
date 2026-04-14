"""TrayDaemon — wraps DaemonRunner with a tray-friendly interface."""

import pathlib
import sys
import threading
import time
from collections.abc import Callable

# Import DaemonRunner from scripts/sidecar.py (not a package, use sys.path)
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import sidecar as _sidecar  # noqa: E402

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

        last_at = runner.last_cycle_at
        if last_at is not None:
            age = int(time.time() - last_at)
            if age < 60:
                parts.append(f"Last sync: {age}s ago")
            else:
                parts.append(f"Last sync: {age // 60}m ago")
        else:
            parts.append("Last sync: never")

        count = runner.last_metrics_count
        parts.append(f"{count} metric{'s' if count != 1 else ''}")

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
            }
        if self.on_status_change is not None:
            self.on_status_change(status)
