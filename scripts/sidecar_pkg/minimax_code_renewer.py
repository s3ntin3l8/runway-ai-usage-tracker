"""Optional keep-alive for the MiniMax Code login (``~/.minimax/auth/<env>/<region>/mcode-public/``).

The MiniMax Code access token lives exactly twelve hours and only the MiniMax Code CLI or the
desktop app renews it, when it next runs. A host whose MiniMax Code has been idle for longer than
that is left with a dead login, and Runway's own MiniMax quota collection (the app's ``mmoat_...``
token against ``platform.minimax.io/v1/api/openplatform/coding_plan/remains``) fails with it. The
provider rotates the refresh token, so the server never refreshes a login a sidecar pushed
(``ROTATING_REFRESH_PROVIDERS`` in ``app/services/refresh_policy.py``): a server-side refresh would
rotate the token out from under the app and sign the user out.

This renewer renews the login **in the CLI's own file** and writes the result back, exactly as the
xAI, Claude Code and Codex renewers do. The request is the CLI's own: a form body of
``grant_type``, ``refresh_token``, ``client_id``, ``scope`` and ``audience`` against the account
service of the login's region (``account.minimax.io`` for ``en``, ``account.minimax.cn`` for ``cn``).

The store the CLI and the desktop app share has two files, and their ``generation`` counters must
stay equal -- the CLI answers ``status: "error"`` when they do not:

``auth.json``
    ``{"schemaVersion": 1, "records": {"<service>\\u0000<account>": {...}}}`` where the record
    holds ``accessToken``, ``refreshToken``, ``tokenType`` (``Bearer``), ``clientId``
    (``mcode-public``), ``scopes`` (``["agent.default"]``), ``audience`` (``agent-backend``),
    ``expiresAtMs`` (epoch milliseconds), ``generation`` and ``loginEpoch``. The service is
    ``com.minimax.mcode.oauth.<buildEnv>.<region>``.

``auth-state.json``
    ``{"schemaVersion": 2, "status": "authenticated", "storeKind": "file", ...,
    "generation": <same number>, "expiresAtMs": <epoch ms>}``. ``storeKind`` may be
    ``"os-keyring"`` instead of ``"file"`` -- that store is not supported here (no keychain
    access), so such a login is skipped.

A sibling ``refresh/`` directory holds the CLI's staged credential while it refreshes; a pending
credential there means the app is mid-refresh and this renewer stays out of the way.

The write-back updates only the token fields, the expiry and the status. The ``generation`` is
deliberately left alone on both sides: the two files must never disagree, and a write that bumped
one of them would break the login for the user. The state's ``status`` becomes ``authenticated``
again (the app reads the live/expired state from the credential's expiry, not from that field).

A write that could not be saved is queued and retried as a *save* -- never as a new exchange, so
a refresh token is never spent twice. The queue keeps the renewer ``due``, which is what lets the
keep-alive thread give the repair a tick.

``MiniMaxCodeRenewer`` plugs into ``KeepAliveThread`` (``--keep-alive`` / ``"keep_alive": true``).
It never logs token material.
"""

import contextlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.oauth_renewal import RefreshRejectedError, atomic_replace_json

__all__ = ["MiniMaxCodeRenewer", "RefreshRejectedError"]

logger = logging.getLogger(__name__)

# The account service per region -- the CLI's own table (MiniMax Code 0.7.6).
TOKEN_ENDPOINT_BY_REGION = {
    "cn": "https://account.minimax.cn/oauth2/token",
    "en": "https://account.minimax.io/oauth2/token",
}
DEFAULT_REGION = "en"
# The public client the CLI registers with; there is no client secret.
CLIENT_ID = "mcode-public"
SCOPE = "agent.default"
AUDIENCE = "agent-backend"
USER_AGENT = "runway-sidecar"

# The prefix of the record key in ``auth.json``: ``<service>\u0000<account>``.
SERVICE_PREFIX = "com.minimax.mcode.oauth."
CLIENT_DIR = "mcode-public"
STATE_FILE = "auth-state.json"
CREDENTIAL_FILE = "auth.json"
# The CLI stages a credential here while it refreshes.
PENDING_REFRESH_FILE = ("refresh", CREDENTIAL_FILE)

# The access token lives twelve hours; renew well before it lapses, and long before the CLI's own
# 30-second validity window, so the two renewers never race.
LEAD_SECONDS = 90 * 60
REQUEST_TIMEOUT_SECONDS = 15
# A file written this recently was written by the app itself: leave it be.
RECENT_WRITE_SECONDS = 30

# The states of ``auth-state.json`` whose login this renewer may renew. Anything else (a login
# being set up, signed out, or mid-refresh) is left alone.
RENEWABLE_STATES = frozenset({"authenticated", "expired"})


def token_endpoint(region: str | None) -> str:
    """The token endpoint for a login's region.

    ``MCODE_OAUTH_TOKEN_ENDPOINT`` overrides it -- the CLI honours the same variable, and it is
    what makes a local stub server possible in the tests.
    """
    override = os.environ.get("MCODE_OAUTH_TOKEN_ENDPOINT", "").strip()
    if override:
        return override
    return TOKEN_ENDPOINT_BY_REGION.get(
        (region or "").lower(), TOKEN_ENDPOINT_BY_REGION[DEFAULT_REGION]
    )


@dataclass
class Login:
    """One MiniMax Code OAuth login found in an ``auth.json``."""

    path: Path
    state_path: Path
    record_key: str
    region: str
    access: str
    refresh: str
    expires_at: float | None  # epoch seconds
    generation: int


def _key(login: Login) -> tuple[str, str]:
    """Identity of a login for the renewer's memos: the same file reached through two paths
    (``~/.minimax`` and a symlink to it) is one login."""
    return (os.path.realpath(login.path), login.record_key)


def read_state(state_path: Path) -> dict[str, Any] | None:
    """The parsed ``auth-state.json``; None when it is missing or unreadable."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _region_from_path(path: Path) -> str | None:
    """The region of ``.../auth/<buildEnv>/<region>/mcode-public/auth.json``."""
    if path.parent.name != CLIENT_DIR:
        return None
    region = path.parent.parent.name
    return region or None


def read_logins(path: Path) -> list[Login]:
    """Every renewable MiniMax Code OAuth record in *path*.

    A record without a refresh token is not a renewable login and is skipped. The region's
    directory decides which account service the login belongs to; the state file's own ``region``
    overrides it (see :meth:`MiniMaxCodeRenewer._discovered`), because the app writes both.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return []
    region = _region_from_path(path)
    logins: list[Login] = []
    for record_key, record in records.items():
        if not isinstance(record_key, str) or not record_key.startswith(SERVICE_PREFIX):
            continue
        if not isinstance(record, dict):
            continue
        refresh = record.get("refreshToken")
        if not isinstance(refresh, str) or not refresh:
            continue
        expires = record.get("expiresAtMs")
        generation = record.get("generation")
        access = record.get("accessToken")
        logins.append(
            Login(
                path=path,
                state_path=path.parent / STATE_FILE,
                record_key=record_key,
                region=region or DEFAULT_REGION,
                access=access if isinstance(access, str) else "",
                refresh=refresh,
                expires_at=expires / 1000 if isinstance(expires, (int, float)) else None,
                generation=generation if isinstance(generation, int) else -1,
            )
        )
    return logins


def login_due(login: Login, *, now: float | None = None, lead: int = LEAD_SECONDS) -> bool:
    """True when the access token is within ``lead`` seconds of expiry.

    An unreadable expiry counts as due -- let the provider decide.
    """
    if login.expires_at is None:
        return True
    return login.expires_at <= (time.time() if now is None else now) + lead


def recently_written(path: Path, *, now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age < RECENT_WRITE_SECONDS


def _pending_refresh_present(login: Login) -> bool:
    """Whether the app has a credential staged for commit (it is mid-refresh)."""
    return login.path.parent.joinpath(*PENDING_REFRESH_FILE).exists()


def _app_is_refreshing(state: dict[str, Any], login: Login) -> bool:
    """Whether the app is mid-refresh: the state carries a refresh, or a credential is staged."""
    if state.get("refresh"):
        return True
    return _pending_refresh_present(login)


def _response_error_code(raw: bytes) -> str:
    """The OAuth error code of a rejected response; an unreadable body just leaves it blank."""
    code = ""
    with contextlib.suppress(ValueError, AttributeError, OSError):
        err = json.loads(raw).get("error")
        code = str(err.get("code") if isinstance(err, dict) else err or "")[:60]
    return code


def request_refresh(login: Login) -> dict[str, Any]:
    """Exchange a refresh token the way the MiniMax Code CLI does.

    Raises ``RefreshRejectedError`` on 400/401/403 (expired, reused or revoked token) and
    ``OSError`` for anything transient (408/429/5xx, a bad body) -- callers back off.
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": login.refresh,
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "audience": AUDIENCE,
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        token_endpoint(login.region),
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        code = _response_error_code(exc.read())
        if exc.code in (400, 401, 403):
            raise RefreshRejectedError(f"HTTP {exc.code} {code}".strip()) from None
        raise OSError(f"HTTP {exc.code}") from None
    except ValueError as exc:
        raise OSError("unreadable token response") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OSError("token response had no access_token")
    token_type = data.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise OSError("token response had no Bearer token type")
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise OSError("token response had no expires_in")
    # A scope that is present but does not carry the grant the CLI needs is a different grant:
    # writing it would break the login the app holds.
    scope = data.get("scope")
    if isinstance(scope, str) and SCOPE not in scope.split():
        raise OSError("token response carried an unexpected scope")
    return data


def write_back(login: Login, response: dict[str, Any]) -> str:
    """Write the renewed tokens into the login's files: ``"written"``, ``"superseded"`` or ``"failed"``.

    Both files are re-read first. If the credential's refresh token or generation is no longer the
    one we exchanged, the app refreshed concurrently and our result is stale -- drop it. Only the
    token fields and the expiry change; the ``generation`` stays as it is on both sides, so the
    pair the CLI reads stays consistent.

    A credential that already carries exactly this result means our own earlier write landed and
    only the state file is still behind it (the state write failed): the state is brought up to
    date and the credential is left alone.
    """
    try:
        data = json.loads(login.path.read_text(encoding="utf-8"))
        mode = login.path.stat().st_mode & 0o777
    except (OSError, ValueError):
        return "failed"
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return "failed"
    record = records.get(login.record_key)
    if not isinstance(record, dict):
        return "superseded"
    if record.get("generation") != login.generation:
        return "superseded"

    new_access = str(response["access_token"])
    # The endpoint rotates the refresh token; keep the new one, or the old one when it returns none.
    new_refresh = str(response.get("refresh_token") or login.refresh)
    current_refresh = record.get("refreshToken")
    if current_refresh == login.refresh:
        credential_written = False
    elif current_refresh == new_refresh and record.get("accessToken") == new_access:
        credential_written = True  # our own earlier write landed; only the state is behind
    else:
        return "superseded"

    state = read_state(login.state_path)
    if state is None:
        return "failed"
    # The state's generation names the active credential; a mismatch means the app moved on.
    if state.get("generation") != login.generation:
        return "superseded"
    if state.get("status") not in RENEWABLE_STATES:
        return "superseded"
    if _app_is_refreshing(state, login):
        return "superseded"  # the app is mid-refresh

    if not credential_written:
        expires_at_ms = int(time.time() * 1000 + float(response["expires_in"]) * 1000)
        record["accessToken"] = new_access
        record["refreshToken"] = new_refresh
        record["tokenType"] = str(response.get("token_type") or "Bearer")
        record["expiresAtMs"] = expires_at_ms
        if not atomic_replace_json(login.path, data, mode, prefix=".auth-"):
            return "failed"
    else:
        # Our own earlier write landed: the credential carries the expiry it was given, and the
        # state takes that same value -- the two files must not disagree about it.
        expires_at_ms = record.get("expiresAtMs")
        if not isinstance(expires_at_ms, int):
            expires_at_ms = int(time.time() * 1000 + float(response["expires_in"]) * 1000)

    state["expiresAtMs"] = expires_at_ms
    # The token is fresh again, so the login is authenticated; the app reads the live/expired
    # state from the credential's expiry, not from this field.
    state["status"] = "authenticated"
    try:
        state_mode = login.state_path.stat().st_mode & 0o777
    except OSError:
        state_mode = 0o600
    if not atomic_replace_json(login.state_path, state, state_mode, prefix=".state-"):
        return "failed"
    return "written"


# One renewal at a time, process-wide: a keep-alive toggled off and on quickly can leave the old
# thread mid-refresh while its replacement starts. Serialised, the second finds the login already
# renewed (no longer due) and does nothing.
_RENEW_LOCK = threading.Lock()


class MiniMaxCodeRenewer:
    """Renews the MiniMax Code login in every ``auth.json`` the sidecar discovers."""

    name = "minimax"

    def __init__(self, targets: Callable[[], list[Path]]) -> None:
        # A callable, evaluated on every tick: login dirs change on config reload.
        self._targets = targets
        self._rejected: dict[
            tuple[str, str], str
        ] = {}  # login -> refresh token the endpoint refused
        # login -> (the refresh token we spent, the response we could not save yet): retry the
        # *save* next tick instead of exchanging again.
        self._unsaved: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        # The logins whose store the keep-alive does not support, so the note is logged once.
        self._unsupported_noted: set[str] = set()

    def _discovered(self) -> list[tuple[Login, dict[str, Any]]]:
        """Every login the sidecar discovers, with the state file that belongs to it."""
        found: list[tuple[Login, dict[str, Any]]] = []
        seen: set[str] = set()
        for path in self._targets():
            real = os.path.realpath(path)
            if real in seen:  # one file reachable through two paths: renew it once
                continue
            seen.add(real)
            state = read_state(Path(path).parent / STATE_FILE)
            if state is None:
                # Without the state file the pair cannot be kept consistent, so the login is left
                # to the app.
                logger.debug("MiniMax Code keep-alive: no %s next to %s", STATE_FILE, path)
                continue
            for login in read_logins(Path(path)):
                # The state file names the region the app itself uses; it wins over the
                # directory's name -- but only when it names a region we know, so a typo or a
                # new region falls back to the directory instead of to the wrong account service.
                region = state.get("region")
                if isinstance(region, str) and region.strip().lower() in TOKEN_ENDPOINT_BY_REGION:
                    login.region = region.strip().lower()
                found.append((login, state))
        return found

    def _due_logins(self) -> list[Login]:
        logins: list[Login] = []
        for login, state in self._discovered():
            if state.get("storeKind") != "file":
                if os.path.realpath(login.path) not in self._unsupported_noted:
                    self._unsupported_noted.add(os.path.realpath(login.path))
                    logger.info(
                        "MiniMax Code keep-alive: the login in %s is in the %s store, which the "
                        "keep-alive does not support yet",
                        login.path,
                        state.get("storeKind"),
                    )
                continue
            if state.get("status") not in RENEWABLE_STATES:
                continue
            # The state's generation names the active credential.
            if login.generation != state.get("generation"):
                continue
            # The app is mid-refresh: it owns the login until it commits.
            if _app_is_refreshing(state, login):
                continue
            # A re-login (new refresh token) lifts the block; the same dead one stays blocked.
            if self._rejected.get(_key(login)) == login.refresh:
                continue
            if login_due(login) and not recently_written(login.path):
                logins.append(login)
        return logins

    def due(self) -> bool:
        """Whether this renewer has work to do: a due login, or a save that is still queued.

        The queued save counts: the keep-alive thread only calls :meth:`renew` when this is
        true, so a write that could not be saved would otherwise wait for the next renewal
        window (hours) instead of the next tick.
        """
        return bool(self._due_logins()) or bool(self._unsaved)

    def renew(self) -> bool:
        """Renew every due login; True unless one failed. Nothing due is not a failure."""
        with _RENEW_LOCK:
            saved = self._finish_unsaved()
            renewed = self._renew_due_logins()
            return renewed and saved

    def _finish_unsaved(self) -> bool:
        """Complete a write that could not be saved before.

        The refresh was already spent, so only the *save* is retried -- never a new exchange. The
        login is no longer due (its credential was written), so this runs over every login the
        sidecar knows, not over the due ones.
        """
        ok = True
        discovered = {_key(login): (login, state) for login, state in self._discovered()}
        for key, (spent, response) in list(self._unsaved.items()):
            found = discovered.get(key)
            if found is None:
                self._unsaved.pop(key, None)
                continue
            login, _state = found
            if login.refresh != spent and login.access != str(response["access_token"]):
                # The app moved on: our result is stale.
                self._unsaved.pop(key, None)
                continue
            outcome = write_back(login, response)
            if outcome == "failed":
                ok = False
                continue
            self._unsaved.pop(key, None)
            if outcome == "written":
                logger.info(
                    "MiniMax Code keep-alive: saved the renewed login to %s (retry)", login.path
                )
        return ok

    def _renew_due_logins(self) -> bool:
        ok = True
        for login in self._due_logins():
            if _key(login) in self._unsaved:
                # A response is queued for this login: its save is retried by _finish_unsaved,
                # and another exchange would spend a second refresh token.
                continue
            try:
                response = request_refresh(login)
            except RefreshRejectedError as exc:
                # A rejection usually means the app refreshed first. Only an unchanged file means
                # the login is really dead.
                if self._refreshed_elsewhere(login):
                    logger.info(
                        "MiniMax Code keep-alive: %s was renewed by MiniMax Code first", login.path
                    )
                    continue
                self._rejected[_key(login)] = login.refresh
                logger.warning(
                    "MiniMax Code keep-alive: refresh rejected for %s (%s) — log in again with "
                    "`mcode login`",
                    login.path,
                    exc,
                )
                ok = False
                continue
            except OSError as exc:
                logger.warning(
                    "MiniMax Code keep-alive: refresh failed for %s: %s", login.path, exc
                )
                ok = False
                continue
            outcome = write_back(login, response)
            if outcome == "written":
                logger.info("MiniMax Code keep-alive renewed the login in %s", login.path)
            elif outcome == "superseded":
                logger.info(
                    "MiniMax Code keep-alive: %s changed while renewing; left as is", login.path
                )
            else:
                # The refresh succeeded but could not be saved: keep the response and retry.
                self._unsaved[_key(login)] = (login.refresh, response)
                logger.warning(
                    "MiniMax Code keep-alive: could not write the renewed login to %s; "
                    "will retry saving it",
                    login.path,
                )
                ok = False
        return ok

    def _refreshed_elsewhere(self, login: Login) -> bool:
        """Whether the login now carries a different refresh token (the app renewed it first)."""
        for current in read_logins(login.path):
            if current.record_key == login.record_key and current.refresh != login.refresh:
                return True
        return False
