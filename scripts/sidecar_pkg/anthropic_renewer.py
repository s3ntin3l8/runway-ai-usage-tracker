"""Optional keep-alive for the Claude Code login (``~/.claude/.credentials.json``).

The Claude access token lives eight hours and only Claude Code renews it, when it next runs.
Anthropic rotates the refresh token on **every** refresh and each one is strictly single-use
(reusing a rotated-away token answers ``400 invalid_grant``; the newest one keeps working). The
server therefore never refreshes a login a sidecar pushed (``ROTATING_REFRESH_PROVIDERS`` in
``app/services/refresh_policy.py``). So the machine renews it itself: this renewer refreshes the
token shortly before it lapses and **writes the result back into Claude Code's own file**.

Verified against a throwaway login (issue #576): Claude Code accepts a file we rewrote, and a
*running* session re-reads it rather than clobbering it with stale in-memory tokens.

The request is exactly Claude Code's own: a JSON body that includes ``scope`` and no other header
than ``Content-Type``. The shape matters — the form-encoded body with extra ``User-Agent`` /
``anthropic-beta`` headers that the server used before was answered with 429 every time.

``AnthropicRenewer`` plugs into ``KeepAliveThread`` (``--keep-alive`` / ``"keep_alive": true``).
It only touches ``claudeAiOauth`` (the sibling ``mcpOAuth`` block, which Claude Code also writes,
and every other key survive), writes atomically, and never logs token material.
"""

import contextlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.oauth_renewal import RefreshRejectedError, atomic_replace_json

__all__ = ["AnthropicRenewer", "RefreshRejectedError"]

logger = logging.getLogger(__name__)

# Mirrors app/services/token_refresher.py (the sidecar cannot import ``app``);
# tests/unit/test_anthropic_renewer.py asserts the two stay equal.
TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # pragma: allowlist secret
USER_AGENT = "runway-sidecar"

# Only used when a login's file carries no ``scopes`` (older logins): the base set Claude Code
# always requests. Requesting a scope the login was never granted is rejected, so no extras here.
DEFAULT_SCOPES = (
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)

# Renew shortly before the token lapses so a push never carries a dead token and a running
# Claude Code normally still finds it fresh.
LEAD_SECONDS = 15 * 60
REQUEST_TIMEOUT_SECONDS = 15
# A file written this recently was just written by Claude Code itself (or another renewer):
# leave it alone so two refreshes never race for one single-use token.
RECENT_WRITE_SECONDS = 30
# Used when a refresh response carries no ``expires_in`` (never seen): short, so we look again soon.
FALLBACK_EXPIRES_IN = 3600


@dataclass
class Login:
    """The Claude login found in one credentials file."""

    path: Path
    refresh: str
    expires_ms: float | None
    scopes: tuple[str, ...]


def read_login(path: Path) -> Login | None:
    """The renewable login in *path*, or None (unreadable, no ``claudeAiOauth``, no refresh token)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    refresh = oauth.get("refreshToken")
    if not isinstance(refresh, str) or not refresh:
        return None
    expires = oauth.get("expiresAt")
    scopes = oauth.get("scopes")
    return Login(
        path=path,
        refresh=refresh,
        expires_ms=float(expires) if isinstance(expires, int | float) else None,
        scopes=tuple(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else (),
    )


def login_due(login: Login, *, now: float | None = None, lead: int = LEAD_SECONDS) -> bool:
    """True when the access token is within ``lead`` seconds of expiry; unreadable counts as due.

    Claude access tokens are opaque (not JWTs), so ``expiresAt`` is the only clock there is.
    """
    if login.expires_ms is None:
        return True
    return login.expires_ms / 1000 <= (time.time() if now is None else now) + lead


def recently_written(path: Path, *, now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age < RECENT_WRITE_SECONDS


def request_refresh(refresh_token: str, scopes: tuple[str, ...] = ()) -> dict[str, Any]:
    """Exchange a refresh token the way Claude Code does.

    Raises ``RefreshRejectedError`` on 400/401/403 (a dead or already-used token) and ``OSError``
    for anything transient (408/429/5xx, a bad body) — callers back off, never hammer.
    """
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": " ".join(scopes or DEFAULT_SCOPES),
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        code = ""
        with contextlib.suppress(ValueError, AttributeError, OSError):
            # The OAuth error code is the diagnosis; an unreadable body just leaves it blank.
            err = json.loads(exc.read()).get("error")
            code = str(err.get("type") if isinstance(err, dict) else err or "")[:60]
        if exc.code in (400, 401, 403):
            raise RefreshRejectedError(f"HTTP {exc.code} {code}".strip()) from None
        raise OSError(f"HTTP {exc.code}") from None
    except ValueError as exc:
        raise OSError("unreadable token response") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OSError("token response had no access_token")
    return data


def write_back(login: Login, response: dict[str, Any]) -> str:
    """Write the renewed tokens into the file: ``"written"``, ``"superseded"`` or ``"failed"``.

    The file is re-read first: if its refresh token is no longer the one we exchanged, Claude Code
    renewed concurrently and our result is stale — drop it. Only ``claudeAiOauth.accessToken``,
    ``refreshToken``, ``expiresAt`` and ``refreshTokenExpiresAt`` (and ``scopes`` when the
    response returns them) change; everything else survives, and the file keeps its mode.
    """
    try:
        data = json.loads(login.path.read_text(encoding="utf-8"))
        mode = login.path.stat().st_mode & 0o777
    except (OSError, ValueError):
        return "failed"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or oauth.get("refreshToken") != login.refresh:
        return "superseded"

    now = time.time()
    oauth["accessToken"] = str(response["access_token"])
    # The old token is spent the moment the endpoint answered: always keep the new one.
    oauth["refreshToken"] = str(response.get("refresh_token") or login.refresh)
    expires_in = response.get("expires_in")
    if not isinstance(expires_in, int | float) or expires_in <= 0:
        # Never let an odd value cost us the new tokens: the old refresh token is already spent.
        expires_in = FALLBACK_EXPIRES_IN
    oauth["expiresAt"] = int((now + float(expires_in)) * 1000)
    refresh_expires_in = response.get("refresh_token_expires_in")
    if isinstance(refresh_expires_in, int | float) and refresh_expires_in > 0:
        oauth["refreshTokenExpiresAt"] = int((now + float(refresh_expires_in)) * 1000)
    scope = response.get("scope")
    if isinstance(scope, str) and scope.split():
        oauth["scopes"] = scope.split()
    return "written" if atomic_replace_json(login.path, data, mode, prefix=".cred-") else "failed"


# One renewal at a time, process-wide: a keep-alive toggled off and on quickly can leave the old
# thread mid-refresh while its replacement starts. Anthropic refresh tokens are single-use, so two
# concurrent refreshes of the same token would race; serialised, the second finds the login
# already renewed (no longer due) and does nothing.
_RENEW_LOCK = threading.Lock()


class AnthropicRenewer:
    """Renews the Claude Code login in every credentials file the sidecar discovers."""

    name = "anthropic"

    def __init__(self, targets: Callable[[], list[Path]]) -> None:
        # A callable, evaluated on every tick: login dirs change on config reload.
        self._targets = targets
        self._rejected: dict[Path, str] = {}  # path -> refresh token the endpoint refused
        # path -> (the refresh token we spent, the response we could not save yet). The exchange
        # already happened and that token is gone, so the response is the only valid login left:
        # keep it and retry the *save* next tick instead of exchanging again.
        self._unsaved: dict[Path, tuple[str, dict[str, Any]]] = {}

    def _due_logins(self) -> list[Login]:
        logins: list[Login] = []
        seen: set[str] = set()
        for path in self._targets():
            real = os.path.realpath(path)
            if real in seen:  # one file reachable through two paths: renew it once
                continue
            seen.add(real)
            login = read_login(Path(path))
            if login is None:
                continue
            # A re-login (new refresh token) lifts the block; the same dead one stays blocked.
            if self._rejected.get(login.path) == login.refresh:
                continue
            if login_due(login) and not recently_written(login.path):
                logins.append(login)
        return logins

    def due(self) -> bool:
        return bool(self._due_logins())

    def renew(self) -> bool:
        """Renew every due login; True unless one failed. Nothing due is not a failure."""
        with _RENEW_LOCK:
            return self._renew_due_logins()

    def _renew_due_logins(self) -> bool:
        ok = True
        for login in self._due_logins():
            pending = self._unsaved.get(login.path)
            if pending is not None and pending[0] == login.refresh:
                response = pending[1]  # only the save failed last time: don't spend another token
                outcome = write_back(login, response)
                if outcome == "failed":
                    ok = False
                    continue
                self._unsaved.pop(login.path, None)
                if outcome == "written":
                    logger.info(
                        "Claude Code keep-alive: saved the renewed login to %s (retry)", login.path
                    )
                continue
            self._unsaved.pop(login.path, None)  # the file moved on: the stored response is stale
            try:
                response = request_refresh(login.refresh, login.scopes)
            except RefreshRejectedError as exc:
                # Refresh tokens are single-use, so a rejection usually means Claude Code
                # refreshed first. Only an unchanged file means the login is really dead.
                current = read_login(login.path)
                if current is not None and current.refresh != login.refresh:
                    logger.info(
                        "Claude Code keep-alive: %s was renewed by Claude Code first", login.path
                    )
                    continue
                self._rejected[login.path] = login.refresh
                logger.warning(
                    "Claude Code keep-alive: refresh rejected for %s (%s) — "
                    "log in again with `claude auth login`",
                    login.path,
                    exc,
                )
                ok = False
                continue
            except OSError as exc:
                logger.warning("Claude Code keep-alive: refresh failed for %s: %s", login.path, exc)
                ok = False
                continue
            outcome = write_back(login, response)
            if outcome == "written":
                logger.info("Claude Code keep-alive renewed the login in %s", login.path)
            elif outcome == "superseded":
                logger.info(
                    "Claude Code keep-alive: %s changed while renewing; left as is", login.path
                )
            else:
                # The refresh succeeded but could not be saved, and the old token is now spent:
                # Claude Code's copy is dead until we save this response. Keep it and retry.
                self._unsaved[login.path] = (login.refresh, response)
                logger.warning(
                    "Claude Code keep-alive: could not write the renewed login to %s; "
                    "will retry saving it",
                    login.path,
                )
                ok = False
            continue
        return ok
