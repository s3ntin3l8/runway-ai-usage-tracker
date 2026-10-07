"""Optional keep-alive for the Codex (ChatGPT) login (``~/.codex/auth.json``).

The Codex access token is a JWT that lives ten days and only the Codex CLI renews it, when it
next runs (about nine days after the last refresh). A CLI idle for longer than that is left with
a dead login. The provider rotates the refresh token on every refresh, so the server never
refreshes a login a sidecar pushed (``ROTATING_REFRESH_PROVIDERS`` in
``app/services/refresh_policy.py``). This renewer refreshes the token once it is within a day of lapsing (or its expiry is unreadable) and
**writes the result back into Codex's own file**.

Verified against a throwaway login (issue #524): the endpoint accepts an early refresh, Codex
accepts a file we rewrote, and a rotated-away refresh token keeps working for a while (a reuse
window, unlike Claude Code's strictly single-use tokens).

The request is Codex's own: a form body of ``grant_type``, ``refresh_token`` and ``client_id`` —
no ``scope`` — and no special headers.

``CodexRenewer`` plugs into ``KeepAliveThread`` (``--keep-alive`` / ``"keep_alive": true``). It
only changes ``tokens.access_token`` / ``refresh_token`` / ``id_token`` and ``last_refresh``
(``auth_mode``, ``OPENAI_API_KEY``, ``account_id`` and unknown keys survive), writes atomically,
and never logs token material.
"""

import contextlib
import datetime
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
from scripts.sidecar_pkg.xai_renewer import jwt_expiry_epoch

__all__ = ["CodexRenewer", "RefreshRejectedError"]

logger = logging.getLogger(__name__)

# Mirrors app/services/token_refresher.py (the sidecar cannot import ``app``);
# tests/unit/test_codex_renewer.py asserts the two stay equal.
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # pragma: allowlist secret
USER_AGENT = "runway-sidecar"

# The access token lives 240 h and Codex renews itself ~216 h in, so a renewer one day before
# expiry only acts for a CLI that has been idle for most of the token's life — and rarely races it.
LEAD_SECONDS = 24 * 3600
REQUEST_TIMEOUT_SECONDS = 15
# A file written this recently was just written by Codex itself (or another renewer): leave it be.
RECENT_WRITE_SECONDS = 30


def _key(path: Path) -> Path:
    """Identity of a login file for the renewer's memos: the same file reached through two paths
    (``~/.codex`` and a ``CODEX_HOME`` pointing at it) is one login."""
    return Path(os.path.realpath(path))


@dataclass
class Login:
    """The Codex login found in one ``auth.json``."""

    path: Path
    refresh: str
    expires_at: float | None


def read_login(path: Path) -> Login | None:
    """The renewable login in *path*, or None (unreadable, no ``tokens``, no refresh token)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return None
    access = tokens.get("access_token")
    # The id_token lives only an hour and is not the clock; the access JWT's ``exp`` is.
    return Login(
        path=path,
        refresh=refresh,
        expires_at=jwt_expiry_epoch(access) if isinstance(access, str) else None,
    )


def login_due(login: Login, *, now: float | None = None, lead: int = LEAD_SECONDS) -> bool:
    """True when the access token is within ``lead`` seconds of expiry; an unreadable expiry is due."""
    if login.expires_at is None:
        return True
    return login.expires_at <= (time.time() if now is None else now) + lead


def recently_written(path: Path, *, now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age < RECENT_WRITE_SECONDS


def request_refresh(refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token the way the Codex CLI does.

    Raises ``RefreshRejectedError`` on 400/401/403 (expired, reused or revoked token) and
    ``OSError`` for anything transient (408/429/5xx, a bad body) — callers back off.
    """
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID}
    ).encode()
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        TOKEN_ENDPOINT,
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
        code = ""
        with contextlib.suppress(ValueError, AttributeError, OSError):
            # The OAuth error code is the diagnosis; an unreadable body just leaves it blank.
            err = json.loads(exc.read()).get("error")
            code = str(err.get("code") if isinstance(err, dict) else err or "")[:60]
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

    The file is re-read first: if its refresh token is no longer the one we exchanged, Codex
    renewed concurrently and our result is stale — drop it. Only ``tokens.access_token``,
    ``refresh_token``, ``id_token`` (when returned) and ``last_refresh`` change.
    """
    try:
        data = json.loads(login.path.read_text(encoding="utf-8"))
        mode = login.path.stat().st_mode & 0o777
    except (OSError, ValueError):
        return "failed"
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict) or tokens.get("refresh_token") != login.refresh:
        return "superseded"

    tokens["access_token"] = str(response["access_token"])
    # The endpoint rotates the refresh token: always keep the new one.
    tokens["refresh_token"] = str(response.get("refresh_token") or login.refresh)
    if response.get("id_token"):
        tokens["id_token"] = str(response["id_token"])
    data["last_refresh"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return "written" if atomic_replace_json(login.path, data, mode, prefix=".auth-") else "failed"


# One renewal at a time, process-wide: a keep-alive toggled off and on quickly can leave the old
# thread mid-refresh while its replacement starts. Serialised, the second finds the login already
# renewed (no longer due) and does nothing.
_RENEW_LOCK = threading.Lock()


class CodexRenewer:
    """Renews the Codex login in every ``auth.json`` the sidecar discovers."""

    name = "chatgpt"

    def __init__(self, targets: Callable[[], list[Path]]) -> None:
        # A callable, evaluated on every tick: login dirs change on config reload.
        self._targets = targets
        self._rejected: dict[Path, str] = {}  # path -> refresh token the endpoint refused
        # path -> (the refresh token we spent, the response we could not save yet): retry the
        # *save* next tick instead of exchanging again.
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
            if self._rejected.get(_key(login.path)) == login.refresh:
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
            pending = self._unsaved.get(_key(login.path))
            if pending is not None and pending[0] == login.refresh:
                outcome = write_back(login, pending[1])  # don't spend another token
                if outcome == "failed":
                    ok = False
                    continue
                self._unsaved.pop(_key(login.path), None)
                if outcome == "written":
                    logger.info(
                        "Codex keep-alive: saved the renewed login to %s (retry)", login.path
                    )
                continue
            self._unsaved.pop(
                _key(login.path), None
            )  # the file moved on: the stored response is stale
            try:
                response = request_refresh(login.refresh)
            except RefreshRejectedError as exc:
                # A rejection usually means Codex refreshed first. Only an unchanged file means
                # the login is really dead.
                current = read_login(login.path)
                if current is not None and current.refresh != login.refresh:
                    logger.info("Codex keep-alive: %s was renewed by Codex first", login.path)
                    continue
                self._rejected[_key(login.path)] = login.refresh
                logger.warning(
                    "Codex keep-alive: refresh rejected for %s (%s) — log in again with "
                    "`codex login`",
                    login.path,
                    exc,
                )
                ok = False
                continue
            except OSError as exc:
                logger.warning("Codex keep-alive: refresh failed for %s: %s", login.path, exc)
                ok = False
                continue
            outcome = write_back(login, response)
            if outcome == "written":
                logger.info("Codex keep-alive renewed the login in %s", login.path)
            elif outcome == "superseded":
                logger.info("Codex keep-alive: %s changed while renewing; left as is", login.path)
            else:
                # The refresh succeeded but could not be saved: keep the response and retry.
                self._unsaved[_key(login.path)] = (login.refresh, response)
                logger.warning(
                    "Codex keep-alive: could not write the renewed login to %s; "
                    "will retry saving it",
                    login.path,
                )
                ok = False
        return ok
