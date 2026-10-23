"""TrayDaemon — wraps DaemonRunner with a tray-friendly interface."""

import sys
import threading
import time
from collections.abc import Callable

from scripts import sidecar as _sidecar

DaemonRunner = _sidecar.DaemonRunner


class TrayDaemon:
    """Wraps DaemonRunner with a tray-friendly interface."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._started = False
        self._lifecycle_lock = threading.RLock()
        self._runner: DaemonRunner = DaemonRunner(config, on_status_change=self._on_runner_status)
        self.on_status_change: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background DaemonRunner."""
        with self._lifecycle_lock:
            if self._started:
                return
            from scripts.sidecar_pkg.keychain_client import reopen_keychain_client

            try:
                reopen_keychain_client()
                self._arm_keep_alive()
                self._runner.start()
            except BaseException:
                _sidecar._KEEP_ALIVE.stop()
                self._runner.stop()
                raise
            self._started = True

    def stop(self) -> None:
        """Stop the background DaemonRunner."""
        with self._lifecycle_lock:
            if not self._started:
                return
            self._started = False
            try:
                _sidecar._KEEP_ALIVE.stop()
            finally:
                self._runner.stop()

    def configure(self, config: dict) -> None:
        """Replace local permissions before reconciling keep-alive policy."""
        with self._lifecycle_lock:
            from scripts.sidecar_pkg.source_consent import POLICY

            POLICY.configure(config)
            self._config = config
            self._runner._config = config
            if self._started:
                self._arm_keep_alive()

    def _arm_keep_alive(self) -> None:
        # #526 accepts macOS Claude only. Other tray renewers stay gated by #548.
        _sidecar._KEEP_ALIVE.arm(
            self._config.get("keep_alive") is True,
            providers={"anthropic"} if sys.platform == "darwin" else set(),
        )

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

        snapshot = runner.stats_snapshot()
        last_sync = snapshot["last_sync_at"]
        last_upload = snapshot["last_upload_at"]
        metrics = snapshot["last_metrics_count"]
        events = snapshot["last_events_count"]

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

        err = snapshot["last_error"]
        if err:
            # Truncate long errors
            parts.append(f"Error: {err[:60]}")

        return " | ".join(parts)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_runner_status(self, status: str) -> None:
        """Invoked by DaemonRunner on each status change; forwards to tray."""
        if self.on_status_change is not None:
            self.on_status_change(status)
