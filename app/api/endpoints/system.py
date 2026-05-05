import logging
import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import (
    is_local_collector_enabled,
    is_local_credential_scraping_enabled,
    settings,
)
from app.core.db import get_session
from app.core.encryption import encryption_service
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.models.db import ProviderConfig, SystemConfig, WebhookConfig
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
    # Check if request is authenticated via proxy or local trust
    x_forwarded_user = request.headers.get("X-Forwarded-User")
    remote_user = request.headers.get("Remote-User")
    user_context = x_forwarded_user or remote_user

    # Admin key bypass for local development matches core/security.py
    # Only trust if client IS localhost AND server is also only listening on localhost
    is_local_trust = bool(
        request.client
        and request.client.host in ("127.0.0.1", "::1")
        and settings.APP_HOST in ("127.0.0.1", "localhost", "::1")
    )

    auth_methods = []
    if settings.ADMIN_API_KEY:
        auth_methods.append("admin_key")

    x_admin_key = request.headers.get("X-Admin-Key")
    is_valid_admin_key = bool(settings.ADMIN_API_KEY and x_admin_key == settings.ADMIN_API_KEY)

    return {
        "project_name": settings.PROJECT_NAME,
        "run_mode": settings.RUN_MODE,
        "app_host": settings.APP_HOST,
        "app_port": settings.APP_PORT,
        "encryption_enabled": encryption_service.is_enabled,
        "local_collector_enabled": is_local_collector_enabled(),
        "local_credential_scraping": is_local_credential_scraping_enabled(),
        "ingest_api_key_is_default": settings.INGEST_API_KEY_IS_INSECURE_DEFAULT,
        "admin_auth_required": settings.ADMIN_API_KEY is not None,
        "auth_methods": auth_methods,
        "user_context": user_context,
        "is_authenticated": bool(
            user_context or is_local_trust or is_valid_admin_key or not settings.ADMIN_API_KEY
        ),
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


@router.post("/force-collect")
@limiter.limit("6/minute")
async def force_collect(request: Request) -> dict[str, Any]:
    """Trigger an immediate collection cycle and update the registry."""
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.models.db import LatestUsage
    from app.services.poller import poller

    try:
        poller.wake()  # reset dormancy before polling
        await poller.poll_now()
        # poll_now() upserts the freshly collected cards into LatestUsage.
        # Count what's there to confirm to the caller how many cards the
        # dashboard will now show.
        with Session(engine) as session:
            cards = session.exec(select(LatestUsage)).all()
        return {"ok": True, "cards": len(cards)}
    except Exception as e:
        logger.error(f"Force collect failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/wake")
@limiter.limit("10/minute")
async def wake_poller(request: Request) -> dict[str, Any]:
    """Reset dormancy state and restore normal polling interval."""
    from app.services.poller import poller

    poller.wake()
    return {"status": "awake"}


@router.get("/token-health")
@limiter.limit("30/minute")
async def get_token_health(request: Request) -> dict[str, Any]:
    """Return health status for all cached credentials."""
    tokens = await token_health_service.get_health()
    return {"tokens": tokens}


@router.get("/debug/raw/{provider_id}")
@limiter.limit("10/minute")
async def get_raw_provider_data(request: Request, provider_id: str) -> dict[str, Any]:
    """
    Run a specific collector and capture raw HTTP responses.
    Useful for troubleshooting provider API changes.
    """
    raw_responses = []

    async def intercept_response(response: httpx.Response):
        await response.aread()
        try:
            data = response.json()
        except Exception:
            data = response.text

        # Mask authorization headers for safety in debug output
        safe_headers = dict(response.headers)
        if "Authorization" in safe_headers:
            safe_headers["Authorization"] = "Bearer [MASKED]"
        if "Cookie" in safe_headers:
            safe_headers["Cookie"] = "[MASKED]"

        raw_responses.append(
            {
                "url": str(response.url),
                "method": response.request.method,
                "status": response.status_code,
                "headers": safe_headers,
                "body": data,
                "timestamp": time.time(),
            }
        )

    try:
        # 1. Ensure collectors are loaded
        await manager._sync_collectors()

        # 2. Find and run the specific collector
        # We look through all active collectors (handles multi-account)
        # Note: manager uses 'smart_collectors' which are wrappers around the actual collectors
        target_collectors = [
            sc.collector
            for key, sc in manager.smart_collectors.items()
            if key.startswith(f"{provider_id}:") or key == provider_id
        ]

        if not target_collectors:
            # Try to get one from registry if not active yet
            collector = manager._create_collector(provider_id)
            if collector:
                target_collectors = [collector]

        if not target_collectors:
            raise HTTPException(
                status_code=404, detail=f"No collector found for provider: {provider_id}"
            )

        async with httpx.AsyncClient(
            event_hooks={"response": [intercept_response]}, timeout=30.0
        ) as client:
            # Run the primary strategy for the first matching collector
            # (In debug mode we usually just care about the strategy logic)
            await target_collectors[0].collect(client)

        return {"provider_id": provider_id, "responses": raw_responses, "timestamp": time.time()}
    except Exception as e:
        logger.error(f"Raw debug collection failed for {provider_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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


@router.delete("/token-health/{provider}/{account_id}")
@limiter.limit("20/minute")
async def delete_token_health_entry(
    request: Request, provider: str, account_id: str, _=Depends(require_admin_key)
) -> dict[str, Any]:
    """Manually remove a token from the cache."""
    ok = await token_health_service.delete_credential(provider, account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found in cache or database")
    return {"ok": True}


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
    return {
        "webhooks": [
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


class _AppConfigUpdate(BaseModel):
    browser_preference: str | None = None
    default_poll_interval_seconds: int | None = None  # 0 = clear override
    local_collector_enabled: bool | None = None
    local_credential_scraping_enabled: bool | None = None


class _DashboardLayout(BaseModel):
    provider_order: list[str] = Field(default_factory=list)
    card_orders: dict[str, list[str]] = Field(default_factory=dict)


class _ProviderConfigUpdate(BaseModel):
    enabled: bool | None = None
    api_key: str | None = None  # empty string = clear, None = no change
    session_cookie: str | None = None  # empty string = clear, None = no change
    account_label: str | None = None
    poll_interval_seconds: int | None = None
    collection_strategies: list[dict] | None = None  # [{"id": "web", "enabled": true}, ...]


@router.get("/provider-configs")
@limiter.limit("30/minute")
async def list_provider_configs(request: Request, session: Session = Depends(get_session)) -> dict:
    """Return all known providers merged with their DB configuration."""
    from app.core.registry import registry

    # Load DB configs keyed by provider_id
    db_rows = session.exec(select(ProviderConfig)).all()
    db_map: dict[str, ProviderConfig] = {r.provider_id: r for r in db_rows}

    # Fetch global default interval
    sys_cfg = session.exec(select(SystemConfig)).first()
    global_poll_interval = sys_cfg.default_poll_interval_seconds if sys_cfg else None

    results = []
    for p_id, (_, name, default_ttl) in manager.collector_registry.items():
        provider_def = registry.get_provider(p_id) or {}
        icon = provider_def.get("icon", _PROVIDER_ICONS.get(p_id, "🔌"))
        db = db_map.get(p_id)
        rules = provider_def.get("rules", [])
        supports_api_key = any(
            any(k in rule.get("mapping", {}).values() for k in ("api_key", "oauth_token"))
            for rule in rules
            if rule.get("type") in ("env", "file", "keychain")
        )
        supports_session_cookie = any(
            any(
                k in rule.get("mapping", {}).values()
                for k in (
                    "session_cookie",
                    "cookie_session",
                    "cookie_sessionKey",
                    "cookie___Secure-next-auth.session-token",
                    "sessionKey",
                )
            )
            for rule in rules
            if rule.get("type") in ("env", "file", "keychain", "cookie")
        )

        poll_source = "default"
        effective_interval = default_ttl
        if db and db.poll_interval_seconds:
            poll_source = "provider_override"
            effective_interval = db.poll_interval_seconds
        elif global_poll_interval:
            poll_source = "global_override"
            effective_interval = global_poll_interval

        results.append(
            {
                "provider_id": p_id,
                "name": name,
                "icon": icon,
                "enabled": db.enabled if db else True,
                "api_key_set": bool(db and db.api_key_encrypted),
                "session_cookie_set": bool(db and db.session_cookie_encrypted),
                "account_label": db.account_label if db else None,
                "poll_interval_seconds": db.poll_interval_seconds if db else None,
                "default_ttl_seconds": default_ttl,
                "effective_poll_interval": effective_interval,
                "poll_interval_source": poll_source,
                "supports_api_key": supports_api_key,
                "supports_session_cookie": supports_session_cookie,
                "api_key_label": provider_def.get("api_key_label"),
                "api_key_help": provider_def.get("api_key_help"),
                "session_cookie_label": provider_def.get("session_cookie_label"),
                "session_cookie_help": provider_def.get("session_cookie_help"),
                # Strategy configuration
                "supported_strategies": manager.get_supported_strategies(p_id),
                "collection_strategies": db.strategies if db else None,
            }
        )

    return {"providers": results}


@router.put("/provider-config/{provider_id}")
@limiter.limit("20/minute")
async def upsert_provider_config(
    request: Request,
    provider_id: str,
    body: _ProviderConfigUpdate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Create or update provider configuration."""
    if provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    row = session.exec(
        select(ProviderConfig).where(ProviderConfig.provider_id == provider_id)
    ).first()
    if row is None:
        row = ProviderConfig(
            provider_id=provider_id,
            enabled=body.enabled if body.enabled is not None else True,
        )
        session.add(row)
        session.flush()

    if body.enabled is not None:
        row.enabled = body.enabled
    if body.account_label is not None:
        row.account_label = body.account_label or None
    if body.poll_interval_seconds is not None:
        row.poll_interval_seconds = (
            body.poll_interval_seconds if body.poll_interval_seconds > 0 else None
        )
    if body.collection_strategies is not None:
        # None list = reset to defaults; empty list = no strategies (disabled all)
        row.strategies = body.collection_strategies if body.collection_strategies else None
    if body.api_key is not None:
        # Empty string = clear the stored key; non-empty = encrypt and store
        val = body.api_key
        if val:
            if val.lower().startswith("bearer "):
                val = val[7:].strip()
            elif "sessionKey=" in val:
                # Only truncate if it's NOT a full multi-cookie header.
                if not ("cf_clearance" in val or "__cf_bm" in val or val.count(";") > 2):
                    for part in val.split(";"):
                        part = part.strip()
                        if part.startswith("sessionKey="):
                            val = part[11:].strip()
                            break
            elif "__Secure-next-auth.session-token=" in val:
                for part in val.split(";"):
                    part = part.strip()
                    if part.startswith("__Secure-next-auth.session-token="):
                        val = part[32:].strip()
                        break

        row.api_key = val if val else None

        # Propagate to token_cache if this is also mapped as an OAuth token
        if row.api_key and provider_id in ("chatgpt", "anthropic", "gemini"):
            tokens = {"oauth_token": row.api_key}

            # For ChatGPT, try to extract the account_id from the token if it's a JWT
            if provider_id == "chatgpt":
                from app.core.utils import IdentityExtractor

                acc_id = IdentityExtractor.get_openai_account_id_from_jwt(row.api_key)
                if acc_id:
                    tokens["account_id"] = acc_id

            # If this looks like a Claude session key or bundle, also map it to cookie slots
            if provider_id == "anthropic" and (
                "sessionKey=" in row.api_key or row.api_key.startswith("sk-ant-sid")
            ):
                tokens["session_cookie"] = row.api_key
                tokens["cookie_sessionKey"] = row.api_key

            await token_cache.store(provider_id, tokens, account_id="default", source="config")
    if body.session_cookie is not None:
        val = body.session_cookie
        if val and ";" in val or "=" in val:
            # Attempt to extract common session tokens from a full cookie string
            found = None
            if provider_id == "anthropic":
                # Only truncate to sessionKey if it's NOT a full multi-cookie header.
                # If there are many cookies (like Cloudflare's cf_clearance), we need the whole string.
                if "cf_clearance" in val or "__cf_bm" in val or val.count(";") > 2:
                    found = None  # Keep the whole string
                else:
                    # Extract sessionKey for a cleaner storage if it's just a couple of cookies
                    for part in val.split(";"):
                        part = part.strip()
                        if part.startswith("sessionKey="):
                            found = part[11:].strip()
                            break
            elif provider_id == "chatgpt":
                # Extract __Secure-next-auth.session-token
                for part in val.split(";"):
                    part = part.strip()
                    if part.startswith("__Secure-next-auth.session-token="):
                        found = part[32:].strip()
                        break
            elif provider_id == "opencode":
                # Extract auth cookie value if user pasted full "auth=<value>" string
                for part in val.split(";"):
                    part = part.strip()
                    if part.startswith("auth="):
                        found = part[5:].strip()
                        break

            if found:
                val = found

        row.session_cookie = val if val else None

        # Propagate to token_cache so collectors can find it immediately
        if row.session_cookie:
            # Map generic session_cookie to all common provider-specific keys
            # to ensure the manual override works across various collector implementations.
            tokens = {
                "session_cookie": row.session_cookie,
                "cookie_session": row.session_cookie,
                "cookie_sessionKey": row.session_cookie,
                "cookie___Secure-next-auth.session-token": row.session_cookie,
            }

            await token_cache.store(provider_id, tokens, account_id="default", source="config")

    session.commit()
    # Trigger immediate sync and collection to reflect changes in dashboard instantly
    try:
        await manager._sync_collectors()
        await manager.collect_one(provider_id)
    except Exception as e:
        logger.warning(f"Failed to trigger sync after config update for {provider_id}: {e}")

    return {"status": "saved"}


@router.get("/app-config")
@limiter.limit("30/minute")
async def get_app_config(request: Request, session: Session = Depends(get_session)) -> dict:
    """Return global application configuration."""
    cfg = session.exec(select(SystemConfig)).first()
    return {
        "browser_preference": (cfg.browser_preference if cfg else None)
        or settings.BROWSER_PREFERENCE,
        "default_poll_interval_seconds": cfg.default_poll_interval_seconds if cfg else None,
        "local_collector_enabled": is_local_collector_enabled(),
        "local_credential_scraping_enabled": is_local_credential_scraping_enabled(),
    }


@router.put("/app-config")
@limiter.limit("10/minute")
async def upsert_app_config(
    request: Request,
    body: _AppConfigUpdate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Update global application configuration."""
    cfg = session.exec(select(SystemConfig)).first()
    if cfg is None:
        cfg = SystemConfig()
        session.add(cfg)
    if body.browser_preference is not None:
        cfg.browser_preference = body.browser_preference or None
    if body.default_poll_interval_seconds is not None:
        cfg.default_poll_interval_seconds = (
            body.default_poll_interval_seconds if body.default_poll_interval_seconds > 0 else None
        )
    if body.local_collector_enabled is not None:
        cfg.local_collector_enabled = body.local_collector_enabled
    if body.local_credential_scraping_enabled is not None:
        cfg.local_credential_scraping_enabled = body.local_credential_scraping_enabled
    session.commit()
    return {"status": "saved"}


@router.get("/dashboard-layout")
@limiter.limit("30/minute")
async def get_dashboard_layout(request: Request, session: Session = Depends(get_session)) -> dict:
    """Return the persisted dashboard layout. Empty default if unset."""
    import json

    cfg = session.exec(select(SystemConfig)).first()
    raw = cfg.dashboard_layout_json if cfg else None
    if not raw:
        return {"provider_order": [], "card_orders": {}}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"provider_order": [], "card_orders": {}}
    return {
        "provider_order": parsed.get("provider_order", []) or [],
        "card_orders": parsed.get("card_orders", {}) or {},
    }


@router.put("/dashboard-layout")
@limiter.limit("30/minute")
async def put_dashboard_layout(
    request: Request,
    body: _DashboardLayout,
    session: Session = Depends(get_session),
) -> dict:
    """Store a new dashboard layout. No admin key — matches other UI-facing settings."""
    import json

    cfg = session.exec(select(SystemConfig)).first()
    if cfg is None:
        cfg = SystemConfig()
        session.add(cfg)
    cfg.dashboard_layout_json = json.dumps(body.model_dump())
    session.commit()
    return {"status": "saved"}
