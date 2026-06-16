"""Admin auth for mutation endpoints.

`resolve_auth` is the single source of truth for "is this caller allowed?"
— shared by the `require_admin_key` dependency and the `/system/settings`
auth probe so the two can never drift. It returns a structured `AuthResult`
(issue #103) carrying both a coarse `actor_type` and, when a reverse proxy
supplies it, the asserted user identity for the audit log.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Cookie, Header, HTTPException, Request

from app.core.config import settings
from app.core.sessions import verify_session

# Name of the HttpOnly cookie minted by POST /auth/session.
SESSION_COOKIE = "runway_session"

# Legacy `actor` strings kept stable for audit-log readers; actor_types that
# aren't listed here label themselves ("localhost", "session", "api-key").
_ACTOR_LABELS = {"none": "no-admin-key-configured"}


@dataclass(frozen=True)
class AuthResult:
    """Outcome of an auth check.

    actor_type ∈ {"localhost","proxy","session","api-key","none"}. actor_id
    is the proxy-asserted user when known. actor_meta carries proxy extras
    (email, groups) destined for the audit log.
    """

    authenticated: bool
    actor_type: str
    actor_id: str | None = None
    actor_meta: dict[str, str] | None = None

    @property
    def actor(self) -> str:
        """Human-readable label for audit_log's legacy `actor` column."""
        if self.actor_id:
            return self.actor_id
        return _ACTOR_LABELS.get(self.actor_type, self.actor_type)


def _proxy_meta(request: Request) -> dict[str, str] | None:
    """Best-effort identity extras a forward-auth proxy may attach."""
    meta: dict[str, str] = {}
    email = request.headers.get("X-Forwarded-Email")
    groups = request.headers.get("X-Forwarded-Groups")
    if email:
        meta["email"] = email
    if groups:
        meta["groups"] = groups
    return meta or None


def resolve_auth(
    request: Request,
    *,
    x_admin_key: str | None,
    x_forwarded_user: str | None,
    remote_user: str | None,
    session_cookie: str | None,
) -> AuthResult:
    """Evaluate the bypass ladder for a request. Pure aside from `verify_session`.

    Order: no-key-configured → localhost trust → reverse-proxy trust →
    session cookie → X-Admin-Key header.
    """
    if settings.ADMIN_API_KEY is None:
        return AuthResult(True, "none")

    client_host = request.client.host if request.client else None

    # 1. Local trust (zero-touch local usage). Only when client IS localhost
    # AND the server is bound to localhost-only.
    if client_host in ("127.0.0.1", "::1") and settings.APP_HOST in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        return AuthResult(True, "localhost")

    # 2. Reverse-proxy trust — gated by an IP allowlist so the user headers
    # can't be forged by an arbitrary client.
    proxy_user = x_forwarded_user or remote_user
    trusted = settings.trusted_proxy_ips
    if trusted and client_host in trusted and proxy_user:
        return AuthResult(True, "proxy", actor_id=proxy_user, actor_meta=_proxy_meta(request))

    # 3. Browser session cookie (admin key already exchanged at /auth/session).
    if verify_session(session_cookie):
        return AuthResult(True, "session")

    # 4. Standard API key — constant-time compare to prevent a timing oracle.
    if x_admin_key is not None and hmac.compare_digest(x_admin_key, settings.ADMIN_API_KEY):
        return AuthResult(True, "api-key")

    return AuthResult(False, "none")


async def require_admin_key(
    request: Request,
    x_admin_key: str = Header(default=None),
    x_forwarded_user: str = Header(default=None, alias="X-Forwarded-User"),
    remote_user: str = Header(default=None, alias="Remote-User"),
    session_cookie: str = Cookie(default=None, alias=SESSION_COOKIE),
) -> None:
    """Gate a mutation on `resolve_auth`. 403 when the caller isn't authenticated.

    Side effect: stashes the full `AuthResult` on `request.state.auth` (and a
    legacy `request.state.admin_actor` string) so the audit log can attribute
    the mutation. See `app.services.audit_log`.
    """
    result = resolve_auth(
        request,
        x_admin_key=x_admin_key,
        x_forwarded_user=x_forwarded_user,
        remote_user=remote_user,
        session_cookie=session_cookie,
    )
    request.state.auth = result
    request.state.admin_actor = result.actor
    if not result.authenticated:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")
