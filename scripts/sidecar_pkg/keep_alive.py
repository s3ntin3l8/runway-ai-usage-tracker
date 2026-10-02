"""Optional keep-alive for the Antigravity (agy) login.

The agy access token lives one hour and only agy itself can renew it — the
token file carries no OAuth client_id, so neither the sidecar nor the server
can refresh it. Without a live agy session the login lapses hourly and quota
collection fails until a human runs agy again (the 2026-10-02 incident: four
lapses in one day).

``KeepAliveThread`` (started with ``--keep-alive``, opt-in; or config
``"keep_alive": true``) runs ``agy models`` once the on-disk access token has
lapsed. Verified 2026-10-02: ``agy models`` rewrites only the access token
and ``token.expiry`` (refresh_token unchanged), makes no model call, refreshes
an expired token in place — and does *not* touch the file while the token is
still valid, which is why the thread polls the tiny file every minute and
only invokes agy once the token has actually expired (bounded dead-token
window of one tick).
"""

import json
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_PATH = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
# Cheap stat + read of a ~1.6 KB file. This bounds the dead-token window to
# one tick after expiry; agy only rewrites the file once the token has lapsed,
# so a longer tick would leave collection failing for longer.
TICK_SECONDS = 60
# Back off after a failed refresh (logged-out CLI, no network) so a
# permanently broken login isn't retried every tick.
RETRY_TICK_SECONDS = 300
# Due only once the access token has actually lapsed (agy ignores calls while
# it is still valid); unreadable expiry counts as due — let the CLI decide.
LEAD_SECONDS = 0
COMMAND_TIMEOUT_SECONDS = 120

# Set by enable() when the daemon starts the thread; the sidecar's pre-expiry
# warning stays quiet for operators who already opted in.
_enabled = False


def enable() -> None:
    global _enabled
    _enabled = True


def is_enabled() -> bool:
    return _enabled


def expiry_epoch(token_path: Path) -> float | None:
    """``token.expiry`` (ISO 8601) as epoch seconds; None when unreadable."""
    try:
        raw = json.loads(token_path.read_text())
        expiry = str((raw.get("token") or {}).get("expiry") or "")
        if not expiry:
            return None
        return datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, TypeError):
        return None


def refresh_due(token_path: Path, *, now: float | None = None, lead: int = LEAD_SECONDS) -> bool:
    """True when a keep-alive run could help.

    The file must exist (a machine that never logged into agy must not be
    prodded into starting a login flow) and its access token must be within
    ``lead`` seconds of expiry. An unreadable expiry counts as due — let the
    CLI itself decide whether anything is needed.
    """
    if not token_path.is_file():
        return False
    expiry = expiry_epoch(token_path)
    if expiry is None:
        return True
    return expiry <= (time.time() if now is None else now) + lead


def resolve_agy() -> str | None:
    """Locate the agy binary: PATH first, then the common per-user installs."""
    found = shutil.which("agy")
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / "agy",
        home / "bin" / "agy",
        home / ".agy" / "bin" / "agy",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def run_refresh(command: list[str]) -> bool:
    """Run the keep-alive command once; True when it succeeded.

    stdout is never logged (the CLI can echo account details there); on
    failure only the exit code and a truncated stderr tail go to the log —
    diagnostics, never token material.
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Antigravity keep-alive failed to run %s: %s", command[0], exc)
        return False
    if result.returncode == 0:
        logger.info("Antigravity keep-alive renewed the agy login via %s", command[0])
        return True
    tail = (result.stderr or result.stdout or "").strip()[-200:]
    logger.warning(
        "Antigravity keep-alive: %s exited %s%s",
        command[0],
        result.returncode,
        f" — {tail}" if tail else "",
    )
    return False


class KeepAliveThread(threading.Thread):
    """Background thread that keeps the agy access token fresh.

    ``command`` defaults to ``[agy, "models"]`` resolved lazily so an agy
    installed after the sidecar started is still picked up. The thread never
    raises out of ``run()``: a keep-alive failure must not take the sidecar
    down with it.
    """

    def __init__(
        self,
        *,
        token_path: Path | None = None,
        command: list[str] | None = None,
        tick_seconds: float = TICK_SECONDS,
        retry_tick_seconds: float = RETRY_TICK_SECONDS,
        lead_seconds: int = LEAD_SECONDS,
    ) -> None:
        super().__init__(name="AntigravityKeepAlive", daemon=True)
        self._token_path = token_path or DEFAULT_TOKEN_PATH
        self._command = command
        self._tick_seconds = tick_seconds
        self._retry_tick_seconds = retry_tick_seconds
        self._lead_seconds = lead_seconds
        self._stop_event = threading.Event()
        self._missing_warned = False

    def stop(self) -> None:
        """Ask the loop to exit; the wait is interruptible so this is prompt."""
        self._stop_event.set()

    def _resolve_command(self) -> list[str] | None:
        if self._command is not None:
            return self._command
        agy = resolve_agy()
        if agy is None:
            if not self._missing_warned:
                self._missing_warned = True
                logger.warning(
                    "Antigravity keep-alive enabled but `agy` was not found on PATH "
                    "or in ~/.local/bin — the agy login will lapse until agy is installed"
                )
            return None
        self._command = [agy, "models"]
        return self._command

    def cycle_once(self) -> float:
        """One tick; returns how long to wait before the next one.

        Not due → normal tick. Due → run the refresh, backing off after a
        failure (or when agy isn't installed) so a broken login isn't hammered.
        """
        if not refresh_due(self._token_path, lead=self._lead_seconds):
            return self._tick_seconds
        command = self._resolve_command()
        if command is None:
            return self._retry_tick_seconds
        if run_refresh(command):
            return self._tick_seconds
        return self._retry_tick_seconds

    def run(self) -> None:
        # First cycle immediately (the token may already be lapsed at
        # startup), then one tick apart.
        while True:
            try:
                wait = self.cycle_once()
            except Exception:
                logger.warning("Antigravity keep-alive tick failed", exc_info=True)
                wait = self._retry_tick_seconds
            if self._stop_event.wait(wait):
                return
