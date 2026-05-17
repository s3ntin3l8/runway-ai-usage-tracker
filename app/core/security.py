"""Optional admin key dependency for mutation endpoints."""

from fastapi import Header, HTTPException, Request

from app.core.config import settings


async def require_admin_key(
    request: Request,
    x_admin_key: str = Header(default=None),
    x_forwarded_user: str = Header(default=None, alias="X-Forwarded-User"),
    remote_user: str = Header(default=None, alias="Remote-User"),
) -> None:
    """Validate X-Admin-Key header or proxy authentication when ADMIN_API_KEY is configured.

    Allows bypass if:
    1. Request is from 127.0.0.1 and server is bound to localhost.
    2. Request originates from an IP in TRUSTED_PROXY_IPS AND carries a
       proxy-asserted user header (X-Forwarded-User, Remote-User). Without
       the IP allowlist the header is trivially forgeable by any client.
    """
    if settings.ADMIN_API_KEY is None:
        return

    # 1. Local trust (zero-touch development/local usage)
    # Only trust if client IS localhost AND server is also only listening on localhost
    if (
        request.client
        and request.client.host in ("127.0.0.1", "::1")
        and settings.APP_HOST in ("127.0.0.1", "localhost", "::1")
    ):
        return

    # 2. Reverse proxy trust — gated by IP allowlist so the headers can't be
    # forged by an arbitrary client. Both the source IP and a user header
    # must be present.
    trusted = settings.trusted_proxy_ips
    if (
        trusted
        and request.client
        and request.client.host in trusted
        and (x_forwarded_user or remote_user)
    ):
        return

    # 3. Standard API key auth
    if x_admin_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")
