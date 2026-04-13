"""Optional admin key dependency for mutation endpoints."""

from fastapi import Header, HTTPException

from app.core.config import settings


async def require_admin_key(x_admin_key: str = Header(default=None)) -> None:
    """Validate X-Admin-Key header when ADMIN_API_KEY is configured.

    No-op when ADMIN_API_KEY is unset (local-first default). When set, any
    mutation endpoint that depends on this function requires a matching header.
    """
    if settings.ADMIN_API_KEY is None:
        return
    if x_admin_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")
