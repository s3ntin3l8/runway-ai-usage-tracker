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
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    kind: str  # "opencode" | "grok"
    access: str
    refresh: str
    expires_ms: float | None  # the file's own expiry field, when it has one


def _grok_entry(data: Any) -> dict[str, Any] | None:
    # Same scope selection as ``_grok_auth_scope_entry`` in scripts/sidecar.py,
    # restricted to entries that can actually be renewed.
    if not isinstance(data, dict):
        return None
    for key, value in data.items():
        if (
            isinstance(key, str)
            and (key.startswith("https://auth.x.ai::") or key == "https://accounts.x.ai/sign-in")
            and isinstance(value, dict)
            and value.get("key")
            and value.get("refresh_token")
        ):
            return value
    return None


def read_login(path: Path, kind: str) -> Login | None:
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


class RefreshRejectedError(Exception):
    """The token endpoint refused the refresh token (``invalid_grant`` etc.)."""


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
        try:
            code = str(json.loads(exc.read()).get("error") or "")[:60]
        except (ValueError, AttributeError, OSError):
            pass
        if 400 <= exc.code < 500:
            raise RefreshRejectedError(f"HTTP {exc.code} {code}".strip()) from None
        raise OSError(f"HTTP {exc.code}") from None
    except ValueError as exc:
        raise OSError("unreadable token response") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OSError("token response had no access_token")
    return data


def write_back(login: Login, token_response: dict[str, Any]) -> bool:
    """Write the renewed tokens into the file; False when the file moved on meanwhile.

    The file is re-read first: if its refresh token is no longer the one we
    exchanged, the CLI renewed concurrently and our result is stale — drop it.
    Only the xAI entry changes; every other key survives, and the file keeps its mode.
    """
    try:
        data = json.loads(login.path.read_text(encoding="utf-8"))
        mode = login.path.stat().st_mode & 0o777
    except (OSError, ValueError):
        return False
    if login.kind == "opencode":
        entry = data.get("xai") if isinstance(data, dict) else None
        current = entry.get("refresh") if isinstance(entry, dict) else None
    else:
        entry = _grok_entry(data)
        current = entry.get("refresh_token") if entry else None
    if entry is None or current != login.refresh:
        return False

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

    fd, tmp = tempfile.mkstemp(dir=login.path.parent, prefix=".auth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, login.path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


class XaiRenewer:
    """Renews the xAI login in OpenCode's / the Grok CLI's auth file."""

    name = "xai"

    def __init__(self, paths: list[tuple[Path, str]] | None = None) -> None:
        self._paths = paths
        self._logged_out: set[Path] = set()

    def _targets(self) -> list[tuple[Path, str]]:
        if self._paths is not None:
            return self._paths
        return [(p, "opencode") for p in OPENCODE_PATHS] + [(_grok_path(), "grok")]

    def _due_logins(self) -> list[Login]:
        logins = []
        for path, kind in self._targets():
            if path in self._logged_out:
                continue
            login = read_login(path, kind)
            if login is not None and login_due(login):
                logins.append(login)
        return logins

    def due(self) -> bool:
        return bool(self._due_logins())

    def renew(self) -> bool:
        """Renew every due login; True when none failed (and at least one ran)."""
        ok = True
        ran = False
        for login in self._due_logins():
            ran = True
            try:
                response = request_refresh(login.refresh)
            except RefreshRejectedError as exc:
                # Logged out / revoked lineage: stop prodding it until the sidecar restarts.
                self._logged_out.add(login.path)
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
            if write_back(login, response):
                logger.info("xAI keep-alive renewed the %s login in %s", login.kind, login.path)
            else:
                logger.info("xAI keep-alive: %s changed while renewing; left as is", login.path)
        return ran and ok
