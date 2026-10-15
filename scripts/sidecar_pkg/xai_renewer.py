"""Optional keep-alive for the xAI (Grok) login held by OpenCode / the Grok CLI.

The xAI access token lives about six hours and only the CLI that owns the login
renews it, when it next runs. The server will not refresh a login a sidecar
pushed (``ROTATING_REFRESH_PROVIDERS`` in ``app/services/refresh_policy.py``):
xAI may rotate the refresh token, and a server-side refresh would sign the CLI
out because the new token never reaches its file. So the machine renews it
itself: this renewer refreshes the token and **writes the result back to the
CLI's own auth file**, which keeps the CLI logged in whether or not xAI rotates.

``XaiRenewer`` plugs into ``KeepAliveThread`` (``--keep-alive`` /
``"keep_alive": true``). It only touches the xAI entry of a file, atomically,
and never logs token material.
"""

import base64
import contextlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.oauth_renewal import RefreshRejectedError, atomic_replace_json
from scripts.sidecar_pkg.opencode_credentials import (
    read_oauth_row,
    update_oauth_value,
    v2_db_for_auth,
)

__all__ = ["RefreshRejectedError", "XaiRenewer", "grok_scope_entry"]

logger = logging.getLogger(__name__)

# Mirrors app/services/token_refresher.py (the sidecar cannot import ``app``);
# tests/unit/test_xai_renewer.py asserts the two stay equal.
TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"  # pragma: allowlist secret
USER_AGENT = "opencode/1.0"

# Renew shortly before the token lapses so a push never carries a dead token.
LEAD_SECONDS = 15 * 60
REQUEST_TIMEOUT_SECONDS = 15

OPENCODE_PATHS = (
    Path.home() / ".local" / "share" / "opencode" / "auth.json",
    Path.home() / ".opencode" / "auth.json",
)


def _grok_path() -> Path:
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok") / "auth.json"


def jwt_expiry_epoch(token: str) -> float | None:
    """``exp`` claim of a JWT as epoch seconds (ms-valued claims normalised)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = float(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return None
    return exp / 1000 if exp > 1e11 else exp


@dataclass
class Login:
    """The xAI tokens found in one auth file."""

    path: Path
    kind: str  # "opencode" | "opencode-db" | "grok"
    access: str
    refresh: str
    expires_ms: float | None  # the file's own expiry field, when it has one
    row_id: str | None = None  # opencode-db: the credential row to compare-and-swap
    # opencode-db: the newer refresh token left in the superseded auth.json, if any.
    fallback_refresh: str | None = None


def grok_scope_entry(data: Any) -> dict[str, Any] | None:
    """Select the Grok OAuth scope entry — the one source of truth for the sidecar's
    credential card, its usage events *and* this renewer, so we renew the token that
    is actually pushed."""
    if not isinstance(data, dict):
        return None
    entries = [
        value
        for key, value in data.items()
        if isinstance(key, str)
        and (key.startswith("https://auth.x.ai::") or key == "https://accounts.x.ai/sign-in")
        and isinstance(value, dict)
    ]
    usable_entries = [entry for entry in entries if entry.get("key")]
    if usable_entries:
        entries = usable_entries
    # Prefer email across scopes, then user ID, so the credential card and
    # usage events agree even if auth.json contains multiple OAuth clients.
    return (
        next((entry for entry in entries if entry.get("email")), None)
        or next((entry for entry in entries if entry.get("user_id")), None)
        or (entries[0] if entries else None)
    )


def _grok_entry(data: Any) -> dict[str, Any] | None:
    entry = grok_scope_entry(data)
    return entry if entry and entry.get("key") and entry.get("refresh_token") else None


def _read_v2_login(db: Path) -> Login | None:
    """The xAI login in opencode v2's credential table, plus the heal fallback."""
    found = read_oauth_row(db, "xai")
    if found is None:
        return None
    row_id, value = found
    access, refresh, expires = value.get("access"), value.get("refresh"), value.get("expires")
    if not isinstance(access, str) or not isinstance(refresh, str) or not refresh:
        return None
    expires_ms = float(expires) if isinstance(expires, int | float) else None
    login = Login(
        path=db,
        kind="opencode-db",
        access=access,
        refresh=refresh,
        expires_ms=expires_ms,
        row_id=row_id,
    )
    # Before v2 took over, the keep-alive kept rotating auth.json, so that stale file can
    # hold the live lineage while the database's token was consumed. Offer it as a fallback
    # only when it is a different, newer login than the row's.
    stale = read_login(db.parent / "auth.json", "opencode")
    if stale is not None and stale.refresh != refresh:
        stale_exp, row_exp = login_expiry(stale), login_expiry(login)
        if stale_exp is not None and (row_exp is None or stale_exp > row_exp):
            login.fallback_refresh = stale.refresh
    return login


def read_login(path: Path, kind: str) -> Login | None:
    if kind == "opencode-db":
        return _read_v2_login(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if kind == "opencode":
        entry = data.get("xai") if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            return None
        access, refresh = entry.get("access"), entry.get("refresh")
        expires = entry.get("expires")
    else:
        entry = _grok_entry(data)
        if entry is None:
            return None
        access, refresh = entry.get("key"), entry.get("refresh_token")
        expires = None
    if not isinstance(access, str) or not isinstance(refresh, str) or not refresh:
        return None
    return Login(
        path=path,
        kind=kind,
        access=access,
        refresh=refresh,
        expires_ms=float(expires) if isinstance(expires, int | float) else None,
    )


def login_expiry(login: Login) -> float | None:
    """Epoch seconds the access token lapses; the JWT ``exp`` wins over the file's field."""
    exp = jwt_expiry_epoch(login.access)
    if exp is not None:
        return exp
    return login.expires_ms / 1000 if login.expires_ms is not None else None


def login_due(login: Login, *, now: float | None = None, lead: int = LEAD_SECONDS) -> bool:
    """True when the token is within ``lead`` seconds of expiry; unreadable expiry counts as due."""
    expiry = login_expiry(login)
    if expiry is None:
        return True
    return expiry <= (time.time() if now is None else now) + lead


def request_refresh(refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token; raises ``RefreshRejectedError`` on a 4xx, ``OSError`` otherwise."""
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID}
    ).encode()
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        TOKEN_ENDPOINT,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        code = ""
        with contextlib.suppress(ValueError, AttributeError, OSError):
            # The OAuth error code is the diagnosis; an unreadable body just leaves it blank.
            code = str(json.loads(exc.read()).get("error") or "")[:60]
        # 408/429 are transient (retry later), not a dead refresh token.
        if exc.code in (400, 401, 403):
            raise RefreshRejectedError(f"HTTP {exc.code} {code}".strip()) from None
        raise OSError(f"HTTP {exc.code}") from None
    except ValueError as exc:
        raise OSError("unreadable token response") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OSError("token response had no access_token")
    return data


def _write_back_v2(login: Login, token_response: dict[str, Any]) -> str:
    """Compare-and-swap the renewed tokens into opencode v2's credential row.

    Only ``access`` / ``refresh`` / ``expires`` change; ``type``, ``methodID`` and
    ``metadata`` survive. A row whose refresh token moved is left alone (``"superseded"``).
    """
    found = read_oauth_row(login.path, "xai")
    if found is None or login.row_id is None:
        return "failed"
    row_id, value = found
    if row_id != login.row_id or value.get("refresh") != login.refresh:
        return "superseded"
    new_access = str(token_response["access_token"])
    updates: dict[str, Any] = {
        "access": new_access,
        "refresh": str(token_response.get("refresh_token") or login.refresh),
    }
    exp = jwt_expiry_epoch(new_access)
    if exp is None and token_response.get("expires_in"):
        exp = time.time() + float(token_response["expires_in"])
    if exp is not None:
        updates["expires"] = int(exp * 1000)
    return update_oauth_value(login.path, row_id, login.refresh, updates)


def write_back(login: Login, token_response: dict[str, Any]) -> str:
    """Write the renewed tokens into the file: ``"written"``, ``"superseded"`` (the file
    moved on meanwhile — nothing to do) or ``"failed"`` (could not read/write it).

    The file is re-read first: if its refresh token is no longer the one we
    exchanged, the CLI renewed concurrently and our result is stale — drop it.
    Only the xAI entry changes; every other key survives, and the file keeps its mode.
    """
    if login.kind == "opencode-db":
        return _write_back_v2(login, token_response)
    try:
        data = json.loads(login.path.read_text(encoding="utf-8"))
        mode = login.path.stat().st_mode & 0o777
    except (OSError, ValueError):
        return "failed"
    if login.kind == "opencode":
        entry = data.get("xai") if isinstance(data, dict) else None
        current = entry.get("refresh") if isinstance(entry, dict) else None
    else:
        entry = _grok_entry(data)
        current = entry.get("refresh_token") if entry else None
    if entry is None or current != login.refresh:
        return "superseded"

    new_access = str(token_response["access_token"])
    new_refresh = str(token_response.get("refresh_token") or login.refresh)
    if login.kind == "opencode":
        entry["access"] = new_access
        entry["refresh"] = new_refresh
        exp = jwt_expiry_epoch(new_access)
        if exp is None and token_response.get("expires_in"):
            exp = time.time() + float(token_response["expires_in"])
        if exp is not None:
            entry["expires"] = int(exp * 1000)
    else:
        entry["key"] = new_access
        entry["refresh_token"] = new_refresh

    # Replaces the real file (not a dotfile-manager symlink), atomically, keeping its mode.
    return "written" if atomic_replace_json(login.path, data, mode, prefix=".auth-") else "failed"


# One renewal at a time, process-wide: a keep-alive toggled off and on quickly can leave the
# old thread mid-refresh while its replacement starts. xAI rotates refresh tokens, so two
# concurrent refreshes of the same token would race; serialised, the second finds the login
# already renewed (no longer due) and does nothing.
_RENEW_LOCK = threading.Lock()


class XaiRenewer:
    """Renews the xAI login in OpenCode's / the Grok CLI's auth file."""

    name = "xai"

    def __init__(self, paths: list[tuple[Path, str]] | None = None) -> None:
        self._paths = paths
        self._rejected: dict[Path, str] = {}  # path -> refresh token the endpoint refused
        self._v2_noted: set[Path] = set()  # opencode v2 dirs already logged as skipped

    def _targets(self) -> list[tuple[Path, str]]:
        if self._paths is not None:
            return self._paths
        targets = [(p, "opencode") for p in OPENCODE_PATHS] + [(_grok_path(), "grok")]
        out: list[tuple[Path, str]] = []
        for path, kind in targets:
            v2_db = self._v2_db(path, kind)
            if kind != "opencode":
                out.append((path, kind))
            elif v2_db is not None and v2_db not in [p for p, _ in out]:
                out.append((v2_db, "opencode-db"))
            elif v2_db is None:
                out.append((path, kind))
        return out

    def _v2_db(self, path: Path, kind: str) -> Path | None:
        """The opencode v2 database that supersedes an opencode ``auth.json``, if any.

        v2 keeps the login in ``opencode.db`` and the file is stale, so the file itself
        is never refreshed or written (xAI rotates refresh tokens: that would fork the
        lineage). The renewer works on the database row instead.
        """
        if kind != "opencode":
            return None
        db = v2_db_for_auth(path)
        if db is not None and path not in self._v2_noted:
            self._v2_noted.add(path)
            logger.info(
                "xAI keep-alive: opencode v2 keeps the login in %s; renewing it there "
                "(the stale auth.json is left alone)",
                db,
            )
        return db

    def _due_logins(self) -> list[Login]:
        logins = []
        for path, kind in self._targets():
            login = read_login(path, kind)
            # A re-login (new refresh token) lifts the block; the same dead one stays blocked.
            if login is not None and self._rejected.get(path) == login.refresh:
                continue
            if login is not None and login_due(login):
                logins.append(login)
        return logins

    def due(self) -> bool:
        return bool(self._due_logins())

    def renew(self) -> bool:
        """Renew every due login; True unless one failed. Nothing due is not a failure."""
        with _RENEW_LOCK:
            return self._renew_due_logins()

    def _refresh(self, login: Login) -> dict[str, Any]:
        """Exchange the login's refresh token, healing a split v2 lineage once.

        If xAI rejects the database's token but the superseded auth.json holds a newer
        one, that is the live lineage: exchange it instead (the result is then written
        into the database by the caller's compare-and-swap).
        """
        try:
            return request_refresh(login.refresh)
        except RefreshRejectedError:
            if not login.fallback_refresh:
                raise
        response = request_refresh(login.fallback_refresh)
        # xAI may keep the refresh token; it must be the one just exchanged, never the
        # database's rejected one that write_back would otherwise fall back to.
        response.setdefault("refresh_token", login.fallback_refresh)
        if not response["refresh_token"]:
            response["refresh_token"] = login.fallback_refresh
        logger.info(
            "xAI keep-alive: adopting the newer login from the superseded auth.json "
            "into opencode.db"
        )
        return response

    def _renew_due_logins(self) -> bool:
        ok = True
        for login in self._due_logins():
            try:
                response = self._refresh(login)
            except RefreshRejectedError as exc:
                # Logged out / revoked lineage: stop prodding it until the sidecar restarts.
                self._rejected[login.path] = login.refresh
                logger.warning(
                    "xAI keep-alive: refresh rejected for %s (%s) — log in again with the CLI",
                    login.path,
                    exc,
                )
                ok = False
                continue
            except OSError as exc:
                logger.warning("xAI keep-alive: refresh failed for %s: %s", login.path, exc)
                ok = False
                continue
            outcome = write_back(login, response)
            if outcome == "written":
                logger.info("xAI keep-alive renewed the %s login in %s", login.kind, login.path)
            elif outcome == "superseded":
                logger.info("xAI keep-alive: %s changed while renewing; left as is", login.path)
            else:
                # The refresh succeeded but could not be saved; if xAI rotated the refresh
                # token the CLI's copy may now be dead. Back off rather than burn another.
                logger.warning(
                    "xAI keep-alive: could not write the renewed login to %s", login.path
                )
                ok = False
        return ok
