from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Dict, Any
import time
import logging

from app.core.config import settings
from app.core.encryption import encryption_service
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.services.collector_manager import manager
from app.services.token_health import token_health_service
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
@limiter.limit("30/minute")
async def health_check(request: Request) -> Dict[str, Any]:
    """Check system health and collector status."""
    return {
        "status": "healthy",
        "timestamp": time.time(),
    }


@router.get("/settings")
@limiter.limit("30/minute")
async def get_app_settings(request: Request) -> Dict[str, Any]:
    """Return the current non-sensitive configuration."""
    return {
        "project_name": settings.PROJECT_NAME,
        "run_mode": settings.RUN_MODE,
        "app_host": settings.APP_HOST,
        "app_port": settings.APP_PORT,
        "encryption_enabled": encryption_service.is_enabled,
        "local_collector_enabled": settings.LOCAL_COLLECTOR_ENABLED,
        "local_credential_scraping": settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED,
        "ingest_api_key_is_default": settings.INGEST_API_KEY_IS_INSECURE_DEFAULT,
    }


@router.get("/status")
@limiter.limit("30/minute")
async def get_collector_status(request: Request) -> Dict[str, Any]:
    """Return detailed health and cache stats for all active collectors."""
    try:
        await manager._sync_collectors()
    except Exception as e:
        logger.error(f"Failed to sync collectors for status: {e}")
    return manager.get_collector_stats()


@router.get("/token-health")
@limiter.limit("30/minute")
async def get_token_health(request: Request) -> Dict[str, Any]:
    """Return health status for all cached credentials."""
    tokens = await token_health_service.get_health()
    return {"tokens": tokens}


@router.post("/token-health/refresh/{provider}/{account_id}")
@limiter.limit("5/minute")
async def refresh_token(
    request: Request,
    provider: str,
    account_id: str,
    _auth: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """Attempt proactive OAuth token refresh for supported providers."""
    tokens = await token_cache.get(provider, account_id) or {}
    if "refresh_token" not in tokens:
        raise HTTPException(status_code=400, detail="No refresh token available")

    from app.services.token_refresher import refresh_oauth_token

    try:
        new_tokens = await refresh_oauth_token(provider, tokens)
        await token_cache.store(provider, new_tokens, account_id)
        return {"status": "refreshed"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Token refresh failed for {provider}/{account_id}: {e}")
        raise HTTPException(status_code=502, detail="Upstream token refresh failed")
