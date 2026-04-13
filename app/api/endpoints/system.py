import logging
import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import get_session
from app.core.encryption import encryption_service
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.models.db import WebhookConfig
from app.models.schemas import LimitCard
from app.services.collector_manager import manager
from app.services.token_cache import token_cache
from app.services.token_health import token_health_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
@limiter.limit("30/minute")
async def health_check(request: Request) -> dict[str, Any]:
    """Check system health and collector status."""
    return {
        "status": "healthy",
        "timestamp": time.time(),
    }


@router.get("/settings")
@limiter.limit("30/minute")
async def get_app_settings(request: Request) -> dict[str, Any]:
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
async def get_collector_status(request: Request) -> dict[str, Any]:
    """Return detailed health and cache stats for all active collectors."""
    try:
        await manager._sync_collectors()
    except Exception as e:
        logger.error(f"Failed to sync collectors for status: {e}")
    return manager.get_collector_stats()


@router.get("/token-health")
@limiter.limit("30/minute")
async def get_token_health(request: Request) -> dict[str, Any]:
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
) -> dict[str, Any]:
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


# --- Webhook alert configuration ---

class _WebhookCreate(BaseModel):
    provider_id: str
    threshold_pct: float = Field(ge=0.0, le=100.0)
    url: str
    channel: Literal["discord", "slack"]
    active: bool = True


class _WebhookUpdate(BaseModel):
    threshold_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    url: str | None = None
    active: bool | None = None


@router.get("/webhooks")
async def list_webhooks(session: Session = Depends(get_session)) -> dict:
    """List all webhook alert configurations."""
    configs = session.exec(select(WebhookConfig)).all()
    return {"webhooks": [
        {
            "id": c.id,
            "provider_id": c.provider_id,
            "threshold_pct": c.threshold_pct,
            "url": c.url,
            "channel": c.channel,
            "active": c.active,
            "last_fired_at": c.last_fired_at.isoformat() if c.last_fired_at else None,
        }
        for c in configs
    ]}


@router.post("/webhooks", status_code=201)
@limiter.limit("10/minute")
async def create_webhook(
    request: Request,
    body: _WebhookCreate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Create a webhook alert configuration."""
    config = WebhookConfig(**body.model_dump())
    session.add(config)
    session.commit()
    session.refresh(config)
    return {"id": config.id}


@router.patch("/webhooks/{webhook_id}")
@limiter.limit("10/minute")
async def update_webhook(
    request: Request,
    webhook_id: int,
    body: _WebhookUpdate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Update a webhook alert configuration."""
    config = session.get(WebhookConfig, webhook_id)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(config, key, value)
    session.add(config)
    session.commit()
    return {"status": "updated"}


@router.delete("/webhooks/{webhook_id}", status_code=204)
@limiter.limit("10/minute")
async def delete_webhook(
    request: Request,
    webhook_id: int,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> None:
    """Delete a webhook alert configuration."""
    config = session.get(WebhookConfig, webhook_id)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")
    session.delete(config)
    session.commit()


@router.post("/webhooks/{webhook_id}/test")
@limiter.limit("5/minute")
async def test_webhook(
    request: Request,
    webhook_id: int,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Fire a test payload to the webhook URL immediately."""
    config = session.get(WebhookConfig, webhook_id)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")

    from app.services.webhooks import _fire_webhook
    test_card = LimitCard(
        service_name="Test Alert",
        icon="T",
        remaining="5%",
        unit="tokens",
        reset="monthly",
        health="warning",
        pace="high",
        detail="",
        provider_id=config.provider_id if config.provider_id != "*" else "test",
        account_id="test-account",
        account_label="Test Account",
        used_value=config.threshold_pct + 5,
        limit_value=100.0,
        data_source="test",
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await _fire_webhook(client, config, test_card, config.threshold_pct + 5)
        return {"status": "sent"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Webhook delivery failed: {e}")
