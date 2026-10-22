"""Optional keep-alive for the Antigravity (agy) login (and, via ``renewers``, xAI).

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

Where the token lives is platform-dependent: a file under the Gemini dir on
POSIX, the Windows Credential Manager on Windows (agy writes no token file
there, and renews the stored entry in place). Both are read through the same
helpers, so the renewal loop is the same everywhere.
"""

import json
import logging
import platform
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.agy_version import resolve_agy

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_PATH = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
# On Windows the agy CLI keeps its OAuth token in the Credential Manager
# (target "gemini:antigravity") and writes no token file, so the keep-alive
# has to read the credential store rather than the filesystem.
WINDOWS_CREDENTIAL_TARGET = "gemini:antigravity"
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

# The provider the built-in (agy) renewer keeps alive; other renewers carry their own `name`.
# KEEP_ALIVE_PROVIDERS (app/services/refresh_policy.py) must equal the union (tested).
AGY_PROVIDER = "antigravity"

# Set by enable() when the daemon starts the thread; the sidecar's pre-expiry
# warning stays quiet for operators who already opted in.
_enabled = False
# Logins switched off individually (a per-login override) while the thread runs for the others.
_disabled_providers: set[str] = set()


def enable() -> None:
    global _enabled
    _enabled = True


def disable() -> None:
    global _enabled, _disabled_providers
    _enabled = False
    _disabled_providers = set()


def set_disabled_providers(providers: Iterable[str]) -> None:
    global _disabled_providers
    _disabled_providers = set(providers)


def is_enabled(provider: str | None = None) -> bool:
    """Whether keep-alive runs; with a *provider*, whether it covers that login."""
    return _enabled and (provider is None or provider not in _disabled_providers)


def _expiry_from_document(document: Any) -> float | None:
    """``token.expiry`` as epoch seconds from a parsed token document."""
    try:
        expiry = str((document.get("token") or {}).get("expiry") or "")
        if not expiry:
            return None
        return datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def expiry_epoch(token_path: Path) -> float | None:
    """``token.expiry`` (ISO 8601) as epoch seconds; None when unreadable."""
    try:
        raw = json.loads(token_path.read_text())
    except (OSError, ValueError):
        return None
    return _expiry_from_document(raw)


def _windows_expiry_epoch() -> float | None:
    """The agy token's expiry, read from the Windows Credential Manager."""
    from scripts.sidecar_pkg.wincred import read_json_credential

    return _expiry_from_document(read_json_credential(WINDOWS_CREDENTIAL_TARGET))


def _windows_expiry_epoch_from(raw: bytes) -> float | None:
    """The agy token's expiry, from a Credential Manager blob already read."""
    from scripts.sidecar_pkg.wincred import decode_json_credential

    return _expiry_from_document(decode_json_credential(raw))


def local_expiry_epoch(token_path: Path | None = None) -> float | None:
    """The expiry of the agy login on this machine, wherever it is stored.

    ``token_path`` names a token file to read (the POSIX layout, and what the
    tests inject). Without one the platform decides: the Windows Credential
    Manager entry on Windows, the token file elsewhere.
    """
    if token_path is not None:
        return expiry_epoch(token_path)
    if platform.system() == "Windows":
        return _windows_expiry_epoch()
    return expiry_epoch(DEFAULT_TOKEN_PATH)


def _due(expiry: float | None, now: float | None, lead: int) -> bool:
    """Whether an access token that expires at *expiry* needs renewing.

    An unreadable expiry counts as due - let the CLI decide. The file and
    the Credential Manager readers both go through this, so the ``lead``
    semantics cannot drift between the platforms.
    """
    if expiry is None:
        return True
    return expiry <= (time.time() if now is None else now) + lead


def refresh_due(token_path: Path, *, now: float | None = None, lead: int = LEAD_SECONDS) -> bool:
    """True when a keep-alive run could help.

    The file must exist (a machine that never logged into agy must not be
    prodded into starting a login flow) and its access token must be within
    ``lead`` seconds of expiry. An unreadable expiry counts as due — let the
    CLI itself decide whether anything is needed. While the file stays
    unparseable the calling loop backs off to its retry interval (see
    ``KeepAliveThread.cycle_once``), so the CLI is not invoked every tick.
    """
    if not token_path.is_file():
        return False
    return _due(expiry_epoch(token_path), now, lead)


def local_refresh_due(
    token_path: Path | None = None, *, now: float | None = None, lead: int = LEAD_SECONDS
) -> bool:
    """Whether the agy login on this machine needs renewing.

    A machine that never logged into agy has nothing to renew and must not
    be prodded into starting a login flow, so an absent token (no file on
    POSIX, no Credential Manager entry on Windows) is not due. A token whose
    expiry cannot be read counts as due — let the CLI decide.
    """
    if token_path is not None:
        return refresh_due(token_path, now=now, lead=lead)
    if platform.system() == "Windows":
        from scripts.sidecar_pkg.wincred import read_credential_bytes

        # One read of the store serves both the presence check and the expiry.
        raw = read_credential_bytes(WINDOWS_CREDENTIAL_TARGET)
        if raw is None:
            return False
        expiry = _windows_expiry_epoch_from(raw)
    else:
        if not DEFAULT_TOKEN_PATH.is_file():
            return False
        expiry = expiry_epoch(DEFAULT_TOKEN_PATH)
    return _due(expiry, now, lead)


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

    ``command`` defaults to ``[agy, "models"]``: the path is resolved lazily
    on the first due tick and retried on every tick while agy is not yet
    installed, so an agy installed after the sidecar started is still picked
    up. Once found, the command is cached. The thread never raises out of
    ``run()``: a keep-alive failure must not take the sidecar down with it.
    """

    def __init__(
        self,
        *,
        token_path: Path | None = None,
        command: list[str] | None = None,
        tick_seconds: float = TICK_SECONDS,
        retry_tick_seconds: float = RETRY_TICK_SECONDS,
        lead_seconds: int = LEAD_SECONDS,
        renewers: list | None = None,
    ) -> None:
        super().__init__(name="AntigravityKeepAlive", daemon=True)
        # Extra renewers (``name``, ``due()``, ``renew()``), e.g. XaiRenewer. Each
        # backs off on its own, so one broken login never delays another.
        self._renewers = list(renewers or [])
        self._renewer_resume_at: dict[str, float] = {}
        # None means "wherever this OS keeps the agy token": the Windows
        # Credential Manager entry on Windows, the token file elsewhere. An
        # explicit path (the tests) always wins.
        self._token_path = token_path
        self._command = command
        self._tick_seconds = tick_seconds
        self._retry_tick_seconds = retry_tick_seconds
        self._lead_seconds = lead_seconds
        self._stop_event = threading.Event()
        self._missing_warned = False
        # Per-login switch (provider id -> on), set by the controller. None = every login is on.
        self.enabled_for: Callable[[str], bool] | None = None

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

    def _provider_on(self, provider: str) -> bool:
        return self.enabled_for is None or self.enabled_for(provider)

    def _cycle_renewers(self) -> None:
        now = time.monotonic()
        for renewer in self._renewers:
            if not self._provider_on(renewer.name):
                continue
            if self._renewer_resume_at.get(renewer.name, 0.0) > now:
                continue
            try:
                if renewer.due() and not renewer.renew():
                    self._renewer_resume_at[renewer.name] = now + self._retry_tick_seconds
            except Exception:
                logger.warning("%s keep-alive tick failed", renewer.name, exc_info=True)
                self._renewer_resume_at[renewer.name] = now + self._retry_tick_seconds

    def cycle_once(self) -> float:
        self._cycle_renewers()
        if not self._provider_on(AGY_PROVIDER):
            return self._tick_seconds
        return self._cycle_agy()

    def _cycle_agy(self) -> float:
        """One agy tick; returns how long to wait before the next one.

        Not due → normal tick. Due → run the refresh, backing off after a
        failure (or when agy isn't installed) so a broken login isn't hammered —
        and also when the CLI exited 0 but the token's expiry is still
        unreadable, so a corrupt token can't be re-prodded every tick.
        """
        if not local_refresh_due(self._token_path, lead=self._lead_seconds):
            return self._tick_seconds
        command = self._resolve_command()
        if command is None:
            return self._retry_tick_seconds
        if run_refresh(command):
            if local_expiry_epoch(self._token_path) is None:
                return self._retry_tick_seconds
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


class KeepAliveController:
    """Starts and stops the keep-alive thread as its desired state changes.

    The sidecar's own flag (``--keep-alive`` / config) is the *local* setting; the server
    can override it per sidecar from the dashboard (``keep_alive_desired`` on every ingest
    response). ``None`` from the server means "no preference" — the local flag decides.
    The server's fleet-wide default (``keep_alive_fleet_default``) sits below that: when no
    per-sidecar override is set, keep-alive runs if the local flag **or** the fleet default is
    on — the default only ever turns it on, it never switches off a sidecar whose own flag is.
    The override is stateless: the server resends it every heartbeat, so nothing is
    written to the sidecar's config and a restart picks it up again on the first check-in.
    Per-login overrides (``keep_alive_desired_providers``) sit on top of all that: a login
    with one follows it, every other login follows the sidecar-level setting. The thread runs
    while any login is on and skips the ones that are off.
    Inert until ``arm`` is called, so one-shot (non-daemon) runs never spawn a thread.
    """

    def __init__(self, make_thread, providers: Callable[[], Iterable[str]] | None = None) -> None:
        self._make_thread = make_thread
        # The login ids this sidecar can keep alive (reported per login to the server).
        self._provider_names = providers or (lambda: ())
        self._providers: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._thread: KeepAliveThread | None = None
        self._armed = False
        self._local = False
        self._remote: bool | None = None
        self._fleet = False
        self._allowed_providers: frozenset[str] | None = None

    @property
    def armed(self) -> bool:
        """True once a daemon start armed it; an unarmed controller cannot renew anything."""
        return self._armed

    def reported(self) -> bool | None:
        """Keep-alive state to report on ingest: ``None`` (unknown) when never armed.

        Startup and unpaired tray instances must not claim keep-alive is off.
        """
        if not self._armed:
            return None
        return self._running if self._allowed_providers is not None else self.effective

    def reported_providers(self) -> dict[str, bool] | None:
        """Keep-alive per login to report on ingest; ``None`` before daemon start."""
        if not self._armed:
            return None
        return {name: self.effective_for(name) for name in self._provider_names()}

    @property
    def effective(self) -> bool:
        """The sidecar-level setting: the Fleet override, else the local flag or fleet default."""
        if self._remote is not None:
            return self._remote
        return self._local or self._fleet

    def effective_for(self, provider: str) -> bool:
        """Whether one login is kept alive: its own override, else the sidecar-level setting."""
        if self._allowed_providers is not None and provider not in self._allowed_providers:
            return False
        override = self._providers.get(provider)
        return self.effective if override is None else override

    @property
    def _running(self) -> bool:
        if self._allowed_providers is not None:
            return any(self.effective_for(name) for name in self._allowed_providers)
        return self.effective or any(self._providers.values())

    def arm(self, local: bool, *, providers: Iterable[str] | None = None) -> None:
        """Start within this entrypoint's capabilities; Fleet cannot expand them."""
        with self._lock:
            self._armed = True
            self._local = bool(local)
            self._allowed_providers = frozenset(providers) if providers is not None else None
            self._reconcile()

    def set_remote(self, desired: bool | None) -> None:
        """Apply the server's setting from an ingest response (no-op until armed)."""
        with self._lock:
            if not self._armed or desired == self._remote:
                return
            self._remote = desired if isinstance(desired, bool) else None
            self._reconcile()

    def set_remote_providers(self, desired: object) -> None:
        """Apply the server's per-login overrides ``{provider_id: bool}`` (no-op until armed).

        Anything that isn't a bool for a string id is dropped; ``None`` / ``{}`` clears them.
        """
        with self._lock:
            overrides = (
                {k: v for k, v in desired.items() if isinstance(k, str) and isinstance(v, bool)}
                if isinstance(desired, dict)
                else {}
            )
            if not self._armed or overrides == self._providers:
                return
            self._providers = overrides
            self._reconcile()

    def set_fleet_default(self, enabled: object) -> None:
        """Apply the server's fleet-wide default from an ingest response (no-op until armed)."""
        with self._lock:
            value = enabled is True
            if not self._armed or value == self._fleet:
                return
            self._fleet = value
            self._reconcile()

    def stop(self) -> None:
        with self._lock:
            self._armed = False
            self._stop_thread()

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread.stop()
            self._thread = None
        disable()

    def _reconcile(self) -> None:
        if self._running and self._thread is None:
            self._thread = self._make_thread()
            self._thread.enabled_for = self.effective_for
            self._thread.start()
            enable()
            logger.info("Keep-alive enabled")
        elif not self._running and self._thread is not None:
            self._stop_thread()
            logger.info("Keep-alive disabled")
        elif not self._running:
            disable()
        if self._running:
            # Logins an override switched off while the thread runs for the others.
            set_disabled_providers(n for n in self._provider_names() if not self.effective_for(n))
