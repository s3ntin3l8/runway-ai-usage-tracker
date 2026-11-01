import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.models._datetime import iso_utc
from app.models.db import (
    ProviderConfig,
    WebhookConfig,
    WebhookCredentialAlert,
)
from app.models.schemas import LimitCard

logger = logging.getLogger(__name__)
router = APIRouter()


class _WebhookCreate(BaseModel):
    provider_id: str
    account_id: str | None = None  # None = applies to all accounts
    threshold_pct: float = Field(ge=0.0, le=100.0)
    url: str
    channel: Literal["discord", "slack"]
    active: bool = True
    credential_alerts: bool = True


class _WebhookUpdate(BaseModel):
    threshold_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    url: str | None = None
    active: bool | None = None
    account_id: str | None = None  # explicit null clears back to "all accounts"
    credential_alerts: bool | None = None


def _validate_webhook_account(session: Session, provider_id: str, account_id: str | None) -> None:
    """Reject account scopes that cannot fire: wildcard providers and
    account_ids with no matching provider_configs row."""
    if account_id is None:
        return
    if provider_id == "*":
        raise HTTPException(
            status_code=400,
            detail="account_id cannot be set when provider_id is '*' (all providers)",
        )
    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == provider_id,
            ProviderConfig.account_id == account_id,
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No provider_configs row for provider={provider_id!r} "
                f"account_id={account_id!r} — configure the account first"
            ),
        )


def _assert_webhook_unique(
    session: Session,
    provider_id: str,
    account_id: str | None,
    url: str,
    exclude_id: int | None = None,
) -> None:
    """Reject duplicate (provider_id, account_id, url) rows.

    SQLite unique indexes treat NULLs as distinct, so the "all accounts"
    (NULL) case must be checked explicitly here alongside the DB index.
    """
    stmt = select(WebhookConfig).where(
        WebhookConfig.provider_id == provider_id,
        WebhookConfig.url == url,
    )
    if account_id is None:
        stmt = stmt.where(col(WebhookConfig.account_id).is_(None))
    else:
        stmt = stmt.where(WebhookConfig.account_id == account_id)
    if exclude_id is not None:
        stmt = stmt.where(WebhookConfig.id != exclude_id)
    if session.exec(stmt).first() is not None:
        raise HTTPException(
            status_code=409,
            detail="A webhook with this provider, account, and URL already exists",
        )


def _webhook_destination_warning(url: str) -> str | None:
    from app.services.webhooks import WebhookURLError, validate_webhook_url

    try:
        validate_webhook_url(url)
    except WebhookURLError:
        return "Unsupported destination. Replace this alert with a Slack or Discord HTTPS webhook."
    return None


@router.get("/webhooks")
async def list_webhooks(
    account_id: str | None = None,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """List webhook alert configurations.

    Optional `account_id` narrows to alerts scoped to that account
    (all-accounts / NULL rows are only returned when the filter is omitted).
    Admin-gated: webhook URLs commonly carry per-channel tokens.
    """
    stmt = select(WebhookConfig)
    if account_id is not None:
        stmt = stmt.where(WebhookConfig.account_id == account_id)
    configs = session.exec(stmt).all()
    return {
        "webhooks": [
            {
                "id": c.id,
                "provider_id": c.provider_id,
                "account_id": c.account_id,
                "threshold_pct": c.threshold_pct,
                "url": c.url,
                "channel": c.channel,
                "active": c.active,
                "credential_alerts": c.credential_alerts,
                "last_fired_at": iso_utc(c.last_fired_at),
                "validation_error": _webhook_destination_warning(c.url),
            }
            for c in configs
        ]
    }


@router.post("/webhooks", status_code=201)
@limiter.limit("10/minute")
async def create_webhook(
    request: Request,
    body: _WebhookCreate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Create a webhook alert configuration."""
    from app.services.webhooks import WebhookURLError, validate_webhook_url

    try:
        validate_webhook_url(body.url)
    except WebhookURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _validate_webhook_account(session, body.provider_id, body.account_id)
    _assert_webhook_unique(session, body.provider_id, body.account_id, body.url)

    config = WebhookConfig(**body.model_dump())
    session.add(config)
    try:
        session.commit()
    except IntegrityError as exc:
        # Concurrent create raced past the pre-check and hit the DB unique index.
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A webhook with this provider, account, and URL already exists",
        ) from exc
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
    from app.services.webhooks import WebhookURLError, validate_webhook_url

    config = session.get(WebhookConfig, webhook_id)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")
    updates = body.model_dump(exclude_none=True)
    if "url" in updates:
        try:
            validate_webhook_url(updates["url"])
        except WebhookURLError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # exclude_none would swallow an explicit account_id: null — that value
    # means "clear back to all accounts", so honor it when the field was set.
    account_touched = "account_id" in body.model_fields_set
    if account_touched:
        updates["account_id"] = body.account_id
    if account_touched or "url" in updates:
        new_provider = config.provider_id
        new_account = body.account_id if account_touched else config.account_id
        new_url = updates.get("url", config.url)
        _validate_webhook_account(session, new_provider, new_account)
        _assert_webhook_unique(session, new_provider, new_account, new_url, exclude_id=webhook_id)
    # A changed account scope or a disabled toggle invalidates any existing
    # credential-alert dedup rows — otherwise a webhook re-scoped to a
    # different account could stay silently "already alerted" for an
    # identity it no longer covers. Compare against the stored value, not
    # just whether the field was sent — a PATCH that merely echoes the
    # current account_id must not wipe an active alert's dedup state.
    account_changed = account_touched and body.account_id != config.account_id
    credential_alerts_turned_off = updates.get("credential_alerts") is False
    if account_changed or credential_alerts_turned_off:
        for alert in session.exec(
            select(WebhookCredentialAlert).where(WebhookCredentialAlert.webhook_id == webhook_id)
        ).all():
            session.delete(alert)
    for key, value in updates.items():
        setattr(config, key, value)
    session.add(config)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A webhook with this provider, account, and URL already exists",
        ) from exc
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
    for alert in session.exec(
        select(WebhookCredentialAlert).where(WebhookCredentialAlert.webhook_id == webhook_id)
    ).all():
        session.delete(alert)
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


# --- Provider configuration ---

# Icons for providers not in registry.json (fallback)
_PROVIDER_ICONS: dict[str, str] = {
    "anthropic": "🟠",
    "gemini": "🔵",
    "github": "🐙",
    "chatgpt": "💬",
    "openrouter": "🚀",
    "minimax": "🤖",
    "kimi_api": "🌙",
    "kimi_coding": "🌙",
    "zai": "🌐",
    "opencode": "⚡",
    "antigravity": "🛸",
    "ollama": "🦙",
}


__all__ = [
    "_WebhookCreate",
    "_WebhookUpdate",
    "_assert_webhook_unique",
    "_validate_webhook_account",
    "_webhook_destination_warning",
    "create_webhook",
    "delete_webhook",
    "list_webhooks",
    "test_webhook",
    "update_webhook",
]
