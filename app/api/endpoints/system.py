import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app import __version__
from app.core.config import settings
from app.core.db import get_session
from app.core.encryption import encryption_service
from app.core.rate_limit import limiter
from app.core.security import SESSION_COOKIE, require_admin_key, resolve_auth
from app.core.utils import scrub_log
from app.models._datetime import iso_utc
from app.models.db import (
    AuditLog,
    LatestUsage,
    ProviderConfig,
    SidecarRegistry,
    SystemConfig,
    WebhookConfig,
)
from app.models.schemas import LimitCard, SidecarDownloadsResponse
from app.services import audit_log
from app.services.account_identity import canonical_account_id
from app.services.collector_manager import manager
from app.services.credential_provider import CredentialProvider
from app.services.sidecar_downloads import sidecar_downloads
from app.services.sidecar_version_checker import is_update_available, sidecar_version_checker
from app.services.token_cache import token_cache
from app.services.token_health import token_health_service

logger = logging.getLogger(__name__)
router = APIRouter()

# Headers to mask in debug output (case-insensitive key match).
_SECRET_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "x-api-key",
        "x-auth-token",
        "x-amz-security-token",
        "x-session-token",
        "x-iam-token",
        "set-cookie",
    }
)


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with known secret values masked."""
    safe = dict(headers)
    for key in safe:
        if key.lower() in _SECRET_HEADERS:
            safe[key] = "[MASKED]"
    return safe


# --- Debug/raw per-strategy helpers -----------------------------------------


def _debug_strategy_label(strategies_dict: dict[str, Any], s_id: str) -> str:
    """Look up the human-readable label for a strategy by id."""
    entry = strategies_dict.get(s_id)
    if entry:
        return entry[0]
    return s_id


def _debug_split_strategies(
    collector: Any,
    dynamic: list[tuple[Any, str]],
) -> tuple[list[tuple[Any, str]], list[tuple[Any, str]]]:
    """Separate resolved strategies into primary and enrichment lists."""
    primary: list[tuple[Any, str]] = []
    enrichment: list[tuple[Any, str]] = []
    for strategy_fn, s_id in dynamic:
        opts = collector._get_strategy_options(s_id)
        if opts.get("enrich"):
            enrichment.append((strategy_fn, s_id))
        else:
            primary.append((strategy_fn, s_id))
    return primary, enrichment


async def _debug_run_one_strategy(
    collector: Any,
    strategy_fn: Any,
    s_id: str,
    kind: str,
) -> dict[str, Any]:
    """Run a single strategy and capture its HTTP traffic."""
    reqs: list[dict[str, Any]] = []
    resps: list[dict[str, Any]] = []
    errs: list[dict[str, Any]] = []

    async def _capture_request(r: httpx.Request) -> None:
        reqs.append(
            {
                "method": r.method,
                "url": str(r.url),
                "headers": _mask_headers(dict(r.headers)),
                "timestamp": time.time(),
            }
        )

    async def _capture_response(r: httpx.Response) -> None:
        await r.aread()
        try:
            data = r.json()
        except Exception:
            data = r.text
        safe_headers = _mask_headers(dict(r.headers))
        resps.append(
            {
                "url": str(r.url),
                "method": r.request.method,
                "status": r.status_code,
                "headers": safe_headers,
                "body": data,
                "timestamp": time.time(),
            }
        )

    card_results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        event_hooks={
            "request": [_capture_request],
            "response": [_capture_response],
        },
        timeout=30.0,
    ) as cl:
        try:
            card_results = await strategy_fn(cl)
        except Exception as exc:
            errs.append({"type": type(exc).__name__, "message": str(exc)})

    is_err = not card_results or any(r.get("remaining") == "ERR" for r in card_results)

    return {
        "label": _debug_strategy_label(collector.STRATEGIES, s_id),
        "kind": kind,
        "status": "error" if errs else ("error" if is_err else "success"),
        "cards_returned": len(card_results),
        "cards_summary": [
            {"service_name": c.get("service_name"), "remaining": c.get("remaining")}
            for c in card_results[:5]
        ],
        "requests": reqs,
        "responses": resps,
        "errors": errs,
    }


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
    """Return non-sensitive configuration plus an authentication probe.

    Kept reachable without auth so the UI can bootstrap its login flow; the
    response itself never includes secrets and redacts the
    `ingest_api_key_is_default` flag from anonymous callers.
    """
    # Share the one auth resolver with require_admin_key so the probe can
    # never report a different verdict than the gate enforces. A valid
    # session cookie, localhost trust, trusted-proxy SSO, or the admin-key
    # header all count. Proxy identity headers are read internally by
    # resolve_auth (their names are configurable via FORWARD_AUTH_*_HEADER).
    auth = resolve_auth(
        request,
        x_admin_key=request.headers.get("X-Admin-Key"),
        session_cookie=request.cookies.get(SESSION_COOKIE),
    )
    is_authenticated = auth.authenticated
    # Only the proxy path carries a user identity to surface to the UI.
    user_context = auth.actor_id if auth.actor_type == "proxy" else None

    auth_methods = []
    if settings.ADMIN_API_KEY:
        auth_methods.append("admin_key")
    if settings.forward_auth_enabled:
        auth_methods.append("forward_auth")

    # The shared version checker caches the repo's latest release tag, which —
    # since release-please tags the whole repo — is also the latest server
    # release. `latest` is None until the first successful GitHub poll (or while
    # offline), and is_update_available treats None as "unknown → don't flag".
    latest = sidecar_version_checker.get_latest()
    response: dict[str, Any] = {
        "project_name": settings.PROJECT_NAME,
        "version": __version__,
        "latest_version": latest,
        "update_available": is_update_available(__version__, latest),
        "app_host": settings.APP_HOST,
        "app_port": settings.APP_PORT,
        "encryption_enabled": encryption_service.is_enabled,
        "admin_auth_required": bool(settings.ADMIN_API_KEY),
        "auth_methods": auth_methods,
        "user_context": user_context,
        "is_authenticated": is_authenticated,
    }
    # Only authenticated callers see the ingest-key warning flag — it's a
    # useful fingerprint for an attacker probing for default deployments.
    if is_authenticated:
        response["ingest_api_key_is_default"] = settings.INGEST_API_KEY_IS_INSECURE_DEFAULT
    return response


@router.get("/status")
@limiter.limit("30/minute")
async def get_collector_status(request: Request) -> dict[str, Any]:
    """Return detailed health and cache stats for all active collectors."""
    try:
        await manager._sync_collectors()
    except Exception as e:
        logger.error(f"Failed to sync collectors for status: {e}")
    return manager.get_collector_stats()


@router.get("/sidecar-downloads", response_model=SidecarDownloadsResponse)
@limiter.limit("30/minute")
async def get_sidecar_downloads(
    request: Request, channel: Literal["stable", "edge"] = "stable"
) -> SidecarDownloadsResponse:
    """Latest sidecar installers + portable builds for *channel* (Fleet page card).

    Public release metadata only (the same data github.com shows anonymously),
    so it needs no admin auth. Cached for an hour; degrades to an ``error``
    field instead of failing when GitHub is unreachable.
    """
    return await sidecar_downloads.get(channel)


@router.get("/audit-log")
@limiter.limit("30/minute")
async def get_audit_log(
    request: Request,
    limit: int = 200,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Return the most recent admin-mutation audit entries.

    Admin-gated: source IPs and action/target metadata are operationally
    sensitive even though the table itself avoids persisting secrets.
    """
    capped = max(1, min(limit, 1000))
    rows = session.exec(select(AuditLog).order_by(col(AuditLog.ts).desc()).limit(capped)).all()
    return {
        "entries": [
            {
                "id": r.id,
                "ts": iso_utc(r.ts) if r.ts else None,
                "actor": r.actor,
                "source_ip": r.source_ip,
                "action": r.action,
                "target_id": r.target_id,
                "payload_json": r.payload_json,
            }
            for r in rows
        ]
    }


@router.post("/force-collect")
@limiter.limit("6/minute")
async def force_collect(
    request: Request,
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Trigger an immediate collection cycle and update the registry.

    Also fans out a pending trigger to every registered sidecar so the next
    sidecar check-in collects everything. Sidecars that are paused
    (collection_enabled=False) are skipped — pausing means "ignore refresh
    requests too" until the user explicitly resumes.
    """
    from sqlmodel import Session, select

    from app.core.cache import cache_clear
    from app.core.db import engine
    from app.models.db import LatestUsage, SidecarRegistry
    from app.services.fleet_registry import fleet_registry
    from app.services.poller import poller

    try:
        poller.wake()  # reset dormancy before polling
        await poller.poll_now()

        sidecars_triggered = 0
        with Session(engine) as session:
            for sc in session.exec(select(SidecarRegistry)).all():
                if sc.collection_enabled:
                    fleet_registry.set_pending_trigger(sc.sidecar_id)
                    sidecars_triggered += 1
            cards = session.exec(select(LatestUsage)).all()
        # poll_now() already updated LatestUsage synchronously above — clear
        # the response cache so a manual "force collect" is visible right away
        # instead of waiting out the fleet/forecast/top-* TTLs.
        cache_clear()
        return {
            "ok": True,
            "cards": len(cards),
            "sidecars_triggered": sidecars_triggered,
        }
    except Exception as e:
        logger.error(f"Force collect failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/check-updates")
@limiter.limit("6/minute")
async def check_updates(
    request: Request,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Force an immediate GitHub release poll for both the server and sidecars.

    Refreshes the shared version-checker cache that `/settings` (server banner)
    and `fleet_registry.to_dict()` (per-sidecar "update available" badges) both
    read, so the user doesn't have to wait for the 24h background poll. Returns
    the server-update verdict; sidecar badges refresh on the next fleet fetch.
    """
    latest = await sidecar_version_checker.check_now()
    audit_log.record(session, request, action="system.check_updates", target_id=None)
    return {
        "current_version": __version__,
        "latest_version": latest,
        "update_available": is_update_available(__version__, latest),
    }


class CleanupRequest(BaseModel):
    clear_cache: bool = True
    prune_snapshots_days: int | None = None
    prune_cumulative_days: int | None = None
    remove_inactive_sidecars_days: int | None = None


@router.post("/cleanup")
@limiter.limit("2/minute")
async def cleanup_database(
    request: Request,
    body: CleanupRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Maintenance: cleanup stale usage records and inactive sidecars."""
    results = {}

    try:
        # 1. Clear LatestUsage cache (Dashboard "ghost" cards)
        if body.clear_cache:
            res = session.exec(delete(LatestUsage))
            results["cache_cleared"] = res.rowcount

        # 2. UsageSnapshot table removed in event-sourced schema reset; prune is a no-op.
        if body.prune_snapshots_days is not None:
            results["snapshots_pruned"] = 0

        # 3. CumulativeUsage table removed in event-sourced schema reset; prune is a no-op.
        if body.prune_cumulative_days is not None:
            results["cumulative_pruned"] = 0

        # 4. Remove inactive sidecars
        if body.remove_inactive_sidecars_days is not None:
            threshold = datetime.now(UTC) - timedelta(days=body.remove_inactive_sidecars_days)
            res = session.exec(delete(SidecarRegistry).where(SidecarRegistry.last_seen < threshold))  # type: ignore[arg-type]
            results["sidecars_removed"] = res.rowcount

        session.commit()

        # Deleted LatestUsage rows / removed sidecars must not keep showing up
        # via a stale fleet/forecast/top-* response cache — clear it whenever
        # this endpoint actually mutated something.
        if body.clear_cache or body.remove_inactive_sidecars_days is not None:
            from app.core.cache import cache_clear

            cache_clear()

        if body.clear_cache:
            # Trigger an immediate background poll to repopulate the cache
            from app.services.poller import poller

            poller.wake()
            asyncio.create_task(poller.poll_now())
            results["poll_triggered"] = True

        return {"ok": True, "results": results}
    except Exception as e:
        logger.error(f"Database cleanup failed: {e}")
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")


@router.post("/wake")
@limiter.limit("10/minute")
async def wake_poller(
    request: Request,
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Reset dormancy state and restore normal polling interval."""
    from app.services.poller import poller

    poller.wake()
    return {"status": "awake"}


@router.get("/token-health")
@limiter.limit("30/minute")
async def get_token_health(
    request: Request,
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Return health status for all cached credentials."""
    tokens = await token_health_service.get_health()
    return {"tokens": tokens}


@router.get("/debug/raw/{provider_id}")
@limiter.limit("10/minute")
async def get_raw_provider_data(
    request: Request,
    provider_id: str,
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """
    Run a specific collector and capture raw HTTP responses.
    Useful for troubleshooting provider API changes.
    Admin-gated: returns live upstream URLs/bodies (auth headers masked).
    """
    raw_requests: list[dict[str, Any]] = []
    raw_responses: list[dict[str, Any]] = []
    collect_errors: list[dict[str, Any]] = []

    async def intercept_request(request: httpx.Request) -> None:
        raw_requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": _mask_headers(dict(request.headers)),
                "timestamp": time.time(),
            }
        )

    async def intercept_response(response: httpx.Response) -> None:
        await response.aread()
        try:
            data = response.json()
        except Exception:
            data = response.text

        safe_headers = _mask_headers(dict(response.headers))

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
        await manager._sync_collectors()

        target_collectors = [
            sc.collector
            for key, sc in manager.smart_collectors.items()
            if key.startswith(f"{provider_id}:") or key == provider_id
        ]

        if not target_collectors:
            collector = manager._create_collector(provider_id)
            if collector:
                target_collectors = [collector]

        if not target_collectors:
            raise HTTPException(
                status_code=404, detail=f"No collector found for provider: {provider_id}"
            )

        collector = target_collectors[0]
        is_configured = await collector.is_configured()

        creds = CredentialProvider.get_credentials(provider_id)
        _cred_key = next((k for k, v in creds.items() if v), None)
        credential_debug: dict[str, Any] = {
            "token_found": bool(_cred_key) or is_configured,
            "token_source": (
                creds.sources.get(_cred_key) if _cred_key else ("cache" if is_configured else None)
            ),
        }

        if hasattr(collector, "reset"):
            await collector.reset()

        strategy_results: dict[str, Any] = {}
        active_strategy: str | None = None
        active_strategy_card_count = 0

        dynamic = collector._resolve_strategies() if collector.STRATEGIES else []

        if not dynamic:
            async with httpx.AsyncClient(
                event_hooks={
                    "request": [intercept_request],
                    "response": [intercept_response],
                },
                timeout=30.0,
            ) as client:
                result: list[dict[str, Any]] = []
                try:
                    result = await collector.collect(client)
                    active_strategy_card_count = len(result) if result else 0
                except Exception as exc:
                    collect_errors.append({"type": type(exc).__name__, "message": str(exc)})
            # Fold legacy collector data into a synthetic strategy entry so
            # the response shape is consistent with the per-strategy path.
            strategy_results["_legacy"] = {
                "label": f"{provider_id} (legacy)",
                "kind": "primary",
                "status": "error" if collect_errors else "success",
                "cards_returned": active_strategy_card_count,
                "cards_summary": [
                    {"service_name": c.get("service_name"), "remaining": c.get("remaining")}
                    for c in (result or [])[:5]
                ],
                "requests": raw_requests,
                "responses": raw_responses,
                "errors": collect_errors,
            }
            return {
                "provider_id": provider_id,
                "is_configured": is_configured,
                "credentials": credential_debug,
                "active_strategy": None,
                "active_strategy_card_count": active_strategy_card_count,
                "strategies": strategy_results,
                "timestamp": time.time(),
            }

        primary_strategies, enrich_strategies = _debug_split_strategies(collector, dynamic)

        for strategy_fn, s_id in primary_strategies:
            strategy_result = await _debug_run_one_strategy(collector, strategy_fn, s_id, "primary")
            strategy_results[s_id] = strategy_result
            if active_strategy is None and strategy_result["status"] == "success":
                active_strategy = s_id
                active_strategy_card_count = strategy_result["cards_returned"]
            await asyncio.sleep(0.5)

        for strategy_fn, s_id in enrich_strategies:
            strategy_result = await _debug_run_one_strategy(
                collector, strategy_fn, s_id, "enrichment"
            )
            strategy_results[s_id] = strategy_result
            await asyncio.sleep(0.5)

        return {
            "provider_id": provider_id,
            "is_configured": is_configured,
            "credentials": credential_debug,
            "active_strategy": active_strategy,
            "active_strategy_card_count": active_strategy_card_count,
            "strategies": strategy_results,
            "timestamp": time.time(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Raw debug collection failed for {scrub_log(provider_id)}: {e}")
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
    cached = await token_cache.get_with_metadata(provider, account_id)
    if not cached:
        raise HTTPException(status_code=404, detail="No cached token for this account")
    tokens, meta = cached
    if "refresh_token" not in tokens:
        raise HTTPException(status_code=400, detail="No refresh token available")

    from app.services.token_refresher import persist_to_local_file, refresh_oauth_token

    try:
        new_tokens = await refresh_oauth_token(provider, tokens)
        await token_cache.store(
            provider,
            new_tokens,
            account_id,
            account_label=meta.get("account_label"),
            source=meta.get("source"),
        )
        persist_to_local_file(provider, new_tokens, meta.get("source"))
        return {"status": "refreshed"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Token refresh failed for {scrub_log(provider)}/{scrub_log(account_id)}: {e}")
        raise HTTPException(status_code=502, detail="Upstream token refresh failed")


@router.delete("/token-health/{provider}/{account_id}")
@limiter.limit("20/minute")
async def delete_token_health_entry(
    request: Request, provider: str, account_id: str, _: None = Depends(require_admin_key)
) -> dict[str, Any]:
    """Manually remove a token from the cache."""
    ok = await token_health_service.delete_credential(provider, account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found in cache or database")
    return {"ok": True}


# --- Webhook alert configuration ---


class _WebhookCreate(BaseModel):
    provider_id: str
    account_id: str | None = None  # None = applies to all accounts
    threshold_pct: float = Field(ge=0.0, le=100.0)
    url: str
    channel: Literal["discord", "slack"]
    active: bool = True


class _WebhookUpdate(BaseModel):
    threshold_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    url: str | None = None
    active: bool | None = None
    account_id: str | None = None  # explicit null clears back to "all accounts"


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
                "last_fired_at": iso_utc(c.last_fired_at),
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
    # IANA tz name (e.g. "Europe/Berlin"); "" or null = clear override (use TZ env / browser detect)
    user_timezone: str | None = None
    # Sidecar update channel: "stable" (default) or "edge"; "" = clear → stable.
    sidecar_update_channel: str | None = None
    # Fleet-wide opt-in: when true, sidecars self-install available updates.
    sidecar_auto_update: bool | None = None


class _DashboardLayout(BaseModel):
    provider_order: list[str] = Field(default_factory=list)
    card_orders: dict[str, list[str]] = Field(default_factory=dict)


class _ProviderConfigUpdate(BaseModel):
    enabled: bool | None = None
    archived: bool | None = None
    api_key: str | None = None  # empty string = clear, None = no change
    session_cookie: str | None = None  # empty string = clear, None = no change
    # Explicit clear flags (PR #287). Mirrors the implicit "empty string = clear"
    # contract above but lets the UI show a dedicated "Clear stored credential"
    # button without ambiguous blank-string semantics. When set to True the
    # corresponding `api_key` / `session_cookie` field is ignored and the
    # stored credential is wiped. Always wins over a same-field write.
    clear_api_key: bool | None = None
    clear_session_cookie: bool | None = None
    account_label: str | None = None
    poll_interval_seconds: int | None = None
    collection_strategies: list[dict] | None = None  # [{"id": "web", "enabled": true}, ...]
    opencode_workspace_id: str | None = None  # empty string clears; None = no change


class _AccountPreviewRequest(BaseModel):
    """Body for POST /provider-config/preview-account (#287).

    The wizard's step 2 debounces keystrokes into this endpoint, derives the
    canonical ``account_id`` from the credential, and surfaces "this
    identity already exists" via 409.
    """

    provider_id: str
    api_key: str | None = None
    session_cookie: str | None = None


@router.get("/provider-configs")
@limiter.limit("30/minute")
async def list_provider_configs(request: Request, session: Session = Depends(get_session)) -> dict:
    """Return all known providers merged with their DB configuration."""
    from app.core.registry import registry

    # Load DB configs grouped by provider_id so the response can expose every
    # row under the new `accounts` field without silently collapsing N rows
    # to one (the multi-account hardening). The legacy top-level fields stay
    # byte-identical for single-row users by sourcing from the canonical row
    # (the `account_id="default"` row when present, else the first row).
    db_rows = session.exec(select(ProviderConfig)).all()
    rows_by_provider: dict[str, list[ProviderConfig]] = {}
    for r in db_rows:
        rows_by_provider.setdefault(r.provider_id, []).append(r)

    # Pre-compute the set of (provider_id, account_id) pairs that have at
    # least one row in latest_usage — drives the per-account `is_orphaned`
    # flag in the response. Single batched DISTINCT query instead of one
    # subquery per row; O(1) lookup when building each entry. Also used
    # below as a durable fallback for passive providers whose credentials
    # live only in token_cache / latest_usage (no provider_configs row).
    from app.models.db import LatestUsage

    live_keys: set[tuple[str, str]] = set()
    live_rows = session.exec(
        select(LatestUsage.provider_id, LatestUsage.account_id).distinct()
    ).all()
    for provider_id, account_id in live_rows:
        if account_id:
            live_keys.add((provider_id, account_id))

    # Active in-memory credentials (sidecar-discovered / server-local).
    # Passive providers (antigravity, opencode-free, …) never get a
    # provider_configs row, so the v2 list must union these in or they
    # render as "unconfigured" while collecting fine (token health shows
    # them working).
    cache_accounts: dict[str, list[tuple[str, str | None]]] = {}
    for c_pid, c_aid, c_name in await token_cache.get_all_active_accounts():
        cache_accounts.setdefault(c_pid, []).append((c_aid, c_name))

    def _canonical_row(rows: list[ProviderConfig]) -> ProviderConfig | None:
        for r in rows:
            if r.account_id == "default":
                return r
        return rows[0] if rows else None

    # Fetch global default interval
    sys_cfg = session.exec(select(SystemConfig)).first()
    global_poll_interval = sys_cfg.default_poll_interval_seconds if sys_cfg else None

    results = []
    for p_id, (_, name, default_ttl) in manager.collector_registry.items():
        provider_def = registry.get_provider(p_id) or {}
        icon = provider_def.get("icon", _PROVIDER_ICONS.get(p_id, "🔌"))
        provider_rows = rows_by_provider.get(p_id, [])
        db = _canonical_row(provider_rows)
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
                    "console_session",
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

        # Per-provider: does any *non-default* account have a `latest_usage`
        # row? Used to gate the `is_orphaned` flag — without a live sibling,
        # the default row's absence from `latest_usage` is equally explained
        # by "just configured" or "collection currently failing", neither of
        # which is a safe-to-remove condition (see flag docs in the response
        # builder below).
        provider_has_live_sibling = any(
            (p_id, r.account_id) in live_keys and r.account_id != "default" for r in provider_rows
        )

        # Union DB rows with cache-only / latest_usage-only identities so
        # passive providers (no provider_configs row by design) still show
        # as configured. DB entries keep `source="config"`; synthetic ones
        # are `source="discovered"`.
        accounts_out: list[dict[str, Any]] = [
            {
                "account_id": r.account_id,
                "enabled": r.enabled,
                "archived": r.archived,
                "api_key_set": bool(r.api_key_encrypted),
                "session_cookie_set": bool(r.session_cookie_encrypted),
                "account_label": r.account_label,
                "poll_interval_seconds": r.poll_interval_seconds,
                "collection_strategies": r.strategies,
                "opencode_workspace_id": r.opencode_workspace_id,
                # `is_orphaned` surfaces the orphaned-bookkeeping-row
                # bug in the settings UI: #286 highlights
                # `account_id="default"` rows that have been shadowed
                # by another account on the same provider. The flag
                # only fires when:
                #   1. the row is the default sentinel
                #   2. the row itself has no live data (missing
                #      from `latest_usage`)
                #   3. AND at least one *other* account on this
                #      provider IS in `latest_usage` — without a
                #      replacement, "just-configured" and
                #      "collection currently failing" rows would be
                #      flagged too, and pairing that with the
                #      destructive Remove button would be a
                #      data-loss prompt on the user's only credential.
                "is_orphaned": (
                    r.account_id == "default"
                    and (p_id, r.account_id) not in live_keys
                    and provider_has_live_sibling
                ),
                "source": "config",
            }
            for r in provider_rows
        ]
        seen_account_ids = {r.account_id for r in provider_rows}
        for c_aid, c_name in cache_accounts.get(p_id, []):
            if c_aid in seen_account_ids:
                continue
            seen_account_ids.add(c_aid)
            accounts_out.append(
                {
                    "account_id": c_aid,
                    "enabled": True,
                    "archived": False,
                    "api_key_set": False,
                    "session_cookie_set": False,
                    "account_label": c_name,
                    "poll_interval_seconds": None,
                    "collection_strategies": None,
                    "opencode_workspace_id": None,
                    "is_orphaned": False,
                    "source": "discovered",
                }
            )
        # Durable fallback for passive providers only (no config rows ever):
        # identities that produced usage but are no longer in the (30-min TTL)
        # token cache — keeps passive status across a server restart until the
        # sidecar re-pushes credentials. Skipped when config rows exist so a
        # deleted account is not resurrected from historical latest_usage.
        if not provider_rows:
            for live_pid, live_aid in sorted(live_keys):
                if live_pid != p_id or live_aid in seen_account_ids:
                    continue
                seen_account_ids.add(live_aid)
                accounts_out.append(
                    {
                        "account_id": live_aid,
                        "enabled": True,
                        "archived": False,
                        "api_key_set": False,
                        "session_cookie_set": False,
                        "account_label": None,
                        "poll_interval_seconds": None,
                        "collection_strategies": None,
                        "opencode_workspace_id": None,
                        "is_orphaned": False,
                        "source": "discovered",
                    }
                )

        results.append(
            {
                "provider_id": p_id,
                "name": name,
                "icon": icon,
                "enabled": db.enabled if db else True,
                "archived": db.archived if db else False,
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
                "opencode_workspace_id": db.opencode_workspace_id if db else None,
                # Per-account breakdown: DB rows first (config-backed), then
                # cache/latest_usage-only identities (discovered). The
                # canonical row above is the first entry whose account_id is
                # "default", or the first entry overall when no default exists.
                "accounts": accounts_out,
                "account_count": len(accounts_out),
            }
        )

    return {"providers": results}


@router.put("/provider-config/{provider_id}/{account_id}")
@limiter.limit("20/minute")
async def upsert_provider_config_for_account(  # noqa: PLR0915 — known-debt: per-field validation + persistence, refactor tracked separately
    request: Request,
    provider_id: str,
    account_id: str,
    body: _ProviderConfigUpdate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Create or update provider configuration for a specific account.

    Multi-account canonical endpoint. The path parameter ``account_id`` is the
    authoritative identity under which credentials are stored and propagated to
    the in-memory token cache; the body may include an ``account_label`` to set
    a human-readable name. Use ``GET /provider-configs`` to discover existing
    ``account_id`` values per provider.
    """
    if provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    # Store under the canonical form every other write path uses, so a
    # typed ``Alice@X.com`` lines up with the cards/events for alice@x.com.
    account_id = canonical_account_id(account_id)
    await _apply_provider_config_update(session, provider_id, account_id, body)
    return {"status": "saved", "provider_id": provider_id, "account_id": account_id}


@router.delete("/provider-config/{provider_id}/{account_id}")
@limiter.limit("20/minute")
async def delete_provider_config_for_account(
    request: Request,
    provider_id: str,
    account_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Remove one ``(provider_id, account_id)`` row and its live ``LatestUsage`` cards.

    The webapp's ``ProviderDetailDialog`` Remove action calls this endpoint
    to clean up orphan ``default`` rows left over from initial setup, and
    any other labeled account the operator no longer wants to track.

    Implementation: **soft-archive** rather than hard-delete. The repo
    convention is that ``provider_configs`` rows never leave the table —
    ``archived=True`` is the established hide-from-dashboard flag, picked
    up by ``_fetch_fleet_view_sync`` in ``app/api/endpoints/usage.py`` at
    both the real-card skip-set (line ~236) and the synthetic-from-events
    skip-set (line ~260). Hard-deleting a row whose ``usage_events`` still
    has rows would let the synthetic loop resurrect the pair as a
    PAYG-style card (PR #317 round-2 review warning).

    Cascades:
      - ``LatestUsage`` cards evicted from ``latest_usage``.
      - stored credentials cleared (``api_key`` / ``session_cookie`` →
        ``None``) — Remove is destructive: the dialog's confirm copy
        promises the stored credentials go with it, and keeping them
        would let a later re-enable re-cache them
        (PR #317 round-2 re-review warning). Note the distinction from
        PUT-archive (``archived: true`` via the ProviderPage archive
        toggle), which only hides the account and keeps credentials
        for easy un-archive.
      - in-memory ``token_cache`` entry dropped so collectors don't keep
        fetching credentials for the removed account.
      - ``credential_tags`` rows whose ``account_id`` matches are deleted
        so the sidecar stops receiving ``origin -> account_id`` hints for
        the removed account on subsequent ``/fleet/config`` heartbeats
        (PR #317 round-2 review warning).
      - ``audit_log`` records ``action="provider_config.delete"``,
        ``target_id=f"{provider_id}/{account_id}"`` (mirrors
        ``sidecar.delete`` + ``credential.tag_set`` conventions).

    ``usage_events`` / rollups are intentionally left in place — they're
    event-sourced history and stay readable via ``/usage/cumulative`` and
    ``/usage/archived-providers`` even without a live card.
    """
    if provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == provider_id,
            ProviderConfig.account_id == account_id,
        )
    ).first()
    if row is None and canonical_account_id(account_id) != account_id:
        # Accept a non-canonical spelling of a canonical row (PUT stores
        # canonical ids); exact match above still wins for legacy rows.
        account_id = canonical_account_id(account_id)
        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == provider_id,
                ProviderConfig.account_id == account_id,
            )
        ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No provider_config for {provider_id}/{account_id}",
        )

    # Evict matching LatestUsage rows so the dashboard doesn't show ghost cards
    # for an account the operator just removed. usage_events / rollups are
    # intentionally left in place — they're event-sourced history and stay
    # readable via /usage/cumulative even without a live card.
    session.exec(
        delete(LatestUsage).where(
            col(LatestUsage.provider_id) == provider_id,
            col(LatestUsage.account_id) == account_id,
        )
    )

    # Drop every credential_tags row tied to this account_id. Otherwise
    # list_pending_payload (app/services/credential_tags.py:124) keeps
    # shipping the hint to sidecars and the /fleet/ingest path stamps
    # every new event with the now-removed account — effectively
    # resurrecting the pair on the dashboard via the synthetic loop.
    from app.services.credential_tags import CredentialTagRepo

    tags_cleared = CredentialTagRepo.delete_by_account(
        session, provider_id=provider_id, account_id=account_id
    )

    # Soft-archive instead of hard-delete (see docstring). Mirrors the
    # PUT helper's archived-toggle cascade: archiving forces enabled=False
    # so no collector keeps polling. Also wipe the stored credential blobs —
    # Remove is destructive (the dialog's confirm copy promises it), and a
    # kept credential could be re-cached if the row were ever re-enabled
    # (PR #317 round-2 re-review warning). PUT-archive deliberately does NOT
    # clear credentials; archive ≠ remove. ``oai_sc_cookie`` is ChatGPT's
    # companion to ``session_cookie`` — the sibling ``clear_session_cookie``
    # path wipes both (parity nit from the round-3 approve review).
    row.archived = True
    row.enabled = False
    row.api_key = None
    row.session_cookie = None
    row.oai_sc_cookie = None
    session.add(row)
    session.commit()

    # Drop the in-memory token cache entry. The async lock is held inside
    # ``remove()``; safe to call from an async endpoint because FastAPI runs
    # the handler on a loop and the cache uses asyncio.Lock.
    await token_cache.remove(provider_id, account_id)

    audit_log.record(
        session,
        request,
        action="provider_config.delete",
        target_id=f"{provider_id}/{account_id}",
        payload={"tags_cleared": tags_cleared},
    )

    # Invalidate cached fleet/limits responses so the next dashboard poll
    # reflects the deletion immediately.
    from app.core.cache import cache_clear

    cache_clear()

    # Trigger an immediate collector sync so the just-removed account's
    # SmartCollector instance (if any) drops out of
    # ``manager.smart_collectors`` — otherwise the next poll could re-
    # write a ``LatestUsage`` card and undo the eviction above. Mirrors
    # the PUT helper's post-commit sync (the ``try/except`` swallows
    # any background error so the user's mutation still succeeds even
    # if the sync itself flakes).
    try:
        await manager._sync_collectors(force=True)
    except Exception as e:
        logger.warning(
            f"Failed to trigger sync after provider_config delete for "
            f"{scrub_log(provider_id)}/{scrub_log(account_id)}: {e}"
        )

    return {
        "status": "deleted",
        "provider_id": provider_id,
        "account_id": account_id,
        "tags_cleared": tags_cleared,
    }


@router.post("/provider-config/preview-account")
@limiter.limit("30/minute")
async def preview_account_identity(
    request: Request,
    body: _AccountPreviewRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Derive the canonical ``account_id`` (and a friendly ``account_label``)
    from a credential the user is about to save. Powers the wizard's step-2
    debounced preview (PR #287).

    Returns ``{suggested_account_id, suggested_label, label_source,
    already_exists}``. On collision returns 409 with the same payload under
    ``detail`` so the wizard can render an inline error.

    ``label_source`` values:
      - ``"email"``: extracted from a JWT claim or pasted email-shaped string.
      - ``"credential_hash"``: PBKDF2-HMAC-SHA256 of the credential
        (fallback when no email is extractable; see
        ``app/services/account_identity.py:resolve_account_id``).
      - ``"default"``: neither email nor hash — falls back to the canonical
        ``"default"`` sentinel.
    """
    import re as _re

    from app.core.utils import IdentityExtractor
    from app.services.account_identity import resolve_account_id

    if body.provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {body.provider_id}")

    credential_hint = body.api_key or body.session_cookie
    if not credential_hint:
        # No credential typed yet — return the canonical "default" so the
        # wizard can render the preview block without a 400.
        return {
            "suggested_account_id": "default",
            "suggested_label": None,
            "label_source": "default",
            "already_exists": _row_exists(session, body.provider_id, "default"),
        }

    email_pattern = r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$"
    email_candidate: str | None = None
    jwt_email = IdentityExtractor.get_email_from_jwt(credential_hint)
    if jwt_email:
        email_candidate = jwt_email.lower()
    elif _re.match(email_pattern, credential_hint):
        email_candidate = credential_hint.lower()

    if email_candidate:
        already_exists = _row_exists(session, body.provider_id, email_candidate)
        if already_exists:
            raise HTTPException(
                status_code=409,
                detail={
                    "suggested_account_id": email_candidate,
                    "suggested_label": email_candidate,
                    "label_source": "email",
                    "already_exists": True,
                },
            )
        return {
            "suggested_account_id": email_candidate,
            "suggested_label": email_candidate,
            "label_source": "email",
            "already_exists": False,
        }

    suggested_account_id = resolve_account_id(
        provider_id=body.provider_id,
        raw_account_id=None,
        account_label=None,
        credential_hint=credential_hint,
    )

    label_source = "credential_hash"
    if _re.match(email_pattern, suggested_account_id):
        label_source = "email"
    if suggested_account_id == "default":
        label_source = "default"

    already_exists = _row_exists(session, body.provider_id, suggested_account_id)
    if already_exists:
        raise HTTPException(
            status_code=409,
            detail={
                "suggested_account_id": suggested_account_id,
                "suggested_label": suggested_account_id if label_source == "email" else None,
                "label_source": label_source,
                "already_exists": True,
            },
        )

    return {
        "suggested_account_id": suggested_account_id,
        "suggested_label": suggested_account_id if label_source == "email" else None,
        "label_source": label_source,
        "already_exists": False,
    }


def _row_exists(session: Session, provider_id: str, account_id: str) -> bool:
    """Cheap EXISTS check used by the preview endpoint. Single-row
    SELECT with LIMIT 1 — faster than loading the full ProviderConfig
    row when we only need to know whether one is there."""
    from sqlmodel import func

    stmt = (
        select(func.count())
        .select_from(ProviderConfig)
        .where(
            ProviderConfig.provider_id == provider_id,
            ProviderConfig.account_id == account_id,
        )
        .limit(1)
    )
    return session.exec(stmt).one() > 0


@router.put("/provider-config/{provider_id}")
@limiter.limit("20/minute")
async def upsert_provider_config(
    request: Request,
    provider_id: str,
    body: _ProviderConfigUpdate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Create or update provider configuration (single-account shortcut).

    Kept permanently as a guarded shortcut for non-webapp callers (operator
    scripts, the legacy ``make sidecar`` helper, third-party integrations).
    Behavior:

    - **0 rows** for this provider: creates a new row with ``account_id="default"``
      — preserves the original "create via this route" behavior so a single-account
      user never has to learn the new path.
    - **1 row**: updates that row in place; ``account_id`` is preserved.
    - **2+ rows**: returns **409 Conflict** with a body pointing at the
      per-account endpoint ``PUT /api/v1/system/provider-config/{provider_id}/{account_id}``
      so the caller can disambiguate.

    For multi-account workflows, call the per-account endpoint directly.
    """
    if provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    rows = session.exec(
        select(ProviderConfig).where(ProviderConfig.provider_id == provider_id)
    ).all()
    if len(rows) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Provider '{provider_id}' has {len(rows)} configured accounts; "
                "use PUT /api/v1/system/provider-config/"
                f"{provider_id}/{{account_id}} to disambiguate."
            ),
        )
    target_account_id = rows[0].account_id if rows else "default"

    await _apply_provider_config_update(session, provider_id, target_account_id, body)
    return {"status": "saved"}


async def _apply_provider_config_update(  # noqa: PLR0915 — known-debt: per-field validation + persistence, refactor tracked separately
    session: Session,
    provider_id: str,
    account_id: str,
    body: _ProviderConfigUpdate,
) -> None:
    """Upsert the ``(provider_id, account_id)`` row and propagate credentials
    to the in-memory token cache under the same ``account_id``.

    Shared core of the two PUT endpoints. Callers are responsible for any
    row-count / account_id guards before invoking this helper.
    """
    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == provider_id,
            ProviderConfig.account_id == account_id,
        )
    ).first()
    if row is None:
        row = ProviderConfig(
            provider_id=provider_id,
            account_id=account_id,
            enabled=body.enabled if body.enabled is not None else True,
        )
        session.add(row)
        session.flush()

    if body.enabled is not None:
        row.enabled = body.enabled
    if body.archived is not None:
        row.archived = body.archived
        # Archiving a provider should stop collection — the user explicitly
        # chose to hide it, so no point wasting poll cycles.
        if body.archived and row.enabled:
            row.enabled = False
        # Unarchiving restores collection so the provider is usable again,
        # but only when the caller did not explicitly state enabled (e.g.
        # the Settings dialog always sends both fields).
        elif body.enabled is None and not body.archived and not row.enabled:
            row.enabled = True
    # PR #317 round-2 re-review warning: enforce the archive invariant AFTER
    # both field assignments. A plain ``{"enabled": true}`` PUT on an
    # archived row (exactly what the dialog's master switch sends for every
    # disabled account) used to leave the row ``archived=True,
    # enabled=True`` — re-caching its credential and respawning a collector
    # while ``archived_pairs`` kept hiding it from the fleet view (invisible
    # collection). An archived row must never be enabled implicitly; the
    # only way out is an explicit ``archived: false`` un-archive, which the
    # assignment above handles before this invariant runs.
    if row.archived and row.enabled:
        row.enabled = False
    if body.account_label is not None:
        row.account_label = body.account_label or None
    if body.poll_interval_seconds is not None:
        row.poll_interval_seconds = (
            body.poll_interval_seconds if body.poll_interval_seconds > 0 else None
        )
    if body.collection_strategies is not None:
        # None list = reset to defaults; empty list = no strategies (disabled all)
        row.strategies = body.collection_strategies if body.collection_strategies else None
    if body.opencode_workspace_id is not None and provider_id == "opencode":
        row.opencode_workspace_id = body.opencode_workspace_id.strip() or None
    if body.clear_api_key is True:
        # Explicit-clear flag wins over any same-field write in the body
        # (the UI sends one or the other, not both). Wipe the stored encrypted
        # blob and invalidate the token-cache entry so stale creds don't linger
        # in collectors that hot-path from cache (PR #287).
        row.api_key = None
        if provider_id in ("opencode", "ollama"):
            await token_cache.remove_tokens(provider_id, account_id, {"api_key", "oauth_token"})
        else:
            await token_cache.remove(provider_id, account_id)
    if body.api_key is not None and body.clear_api_key is not True:
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

        if not val:
            # Documented empty-string clear (the API/script path — the UI
            # sends clear_api_key) must invalidate the cache mirror too, or
            # collectors keep using the removed key until its TTL expires.
            # Same shape as the clear_api_key branch above (PR #287).
            if provider_id in ("opencode", "ollama"):
                await token_cache.remove_tokens(provider_id, account_id, {"api_key", "oauth_token"})
            else:
                await token_cache.remove(provider_id, account_id)

        # Propagate to token_cache if this is also mapped as an OAuth token.
        # Stamp under the resolved account_id (no longer hard-coded "default")
        # so the new per-account endpoint keeps credentials and identity aligned.
        if row.api_key and provider_id in (
            "chatgpt",
            "anthropic",
            "gemini",
            "ollama",
            "kimi_coding",
        ):
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

            # Ollama reads the API key under the "api_key" token-cache slot.
            if provider_id == "ollama":
                tokens["api_key"] = row.api_key

            # Kimi Coding is the same pattern: the collector resolves a
            # dashboard paste from the api_key slot (issue #343) — without
            # this mirror an account-keyed row never reaches the collector.
            if provider_id == "kimi_coding":
                tokens["api_key"] = row.api_key

            await token_cache.store(provider_id, tokens, account_id=account_id, source="config")
        elif row.api_key and provider_id == "xai":
            # A dashboard paste is an access bearer, not a refresh token.
            await token_cache.store(
                provider_id,
                {"xai_access": row.api_key},
                account_id=account_id,
                source="config",
            )
    oai_sc_val: str | None = None  # may be extracted from pasted cookie string below
    if body.clear_session_cookie is True:
        # Mirror of the clear_api_key path above — wipe both session_cookie
        # and the oai-sc companion (ChatGPT-only) and invalidate cache.
        row.session_cookie = None
        row.oai_sc_cookie = None
        if provider_id in ("opencode", "ollama"):
            await token_cache.remove_tokens(
                provider_id,
                account_id,
                {
                    "session_cookie",
                    "cookie_session",
                    "cookie_sessionKey",
                    "cookie___Secure-next-auth.session-token",
                    "console_session",
                },
            )
        else:
            await token_cache.remove(provider_id, account_id)
    if body.session_cookie is not None and body.clear_session_cookie is not True:
        val = body.session_cookie
        if val and (";" in val or "=" in val):
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
                # Handle both monolithic and NextAuth.js chunked (.0 / .1) session tokens.
                # Also extract oai-sc if present — it is required by /api/auth/session.
                chunk0: str | None = None
                chunk1: str | None = None
                for part in val.split(";"):
                    part = part.strip()
                    if part.startswith("__Secure-next-auth.session-token.0="):
                        # len("__Secure-next-auth.session-token.0=") == 35
                        chunk0 = part[35:]
                    elif part.startswith("__Secure-next-auth.session-token.1="):
                        chunk1 = part[35:]
                    elif part.startswith("__Secure-next-auth.session-token="):
                        # len("__Secure-next-auth.session-token=") == 33
                        found = part[33:]
                    elif part.startswith("oai-sc="):
                        oai_sc_val = part[7:]
                if chunk0:
                    found = chunk0 + (chunk1 or "")
            elif provider_id == "opencode":
                # If the pasted string contains __Host-console_session,
                # keep the FULL string in the DB column — the split below
                # pulls both cookies out of it. Otherwise (bare auth value
                # or only the auth cookie) collapse to the bare auth value
                # for backwards compatibility.
                if "__Host-console_session=" in val:
                    found = val
                else:
                    for part in val.split(";"):
                        part = part.strip()
                        if part.startswith("auth="):
                            found = part[5:].strip()
                            break

            if found:
                val = found

        row.session_cookie = val if val else None

        # Persist oai-sc alongside session cookie (ChatGPT only)
        if provider_id == "chatgpt":
            row.oai_sc_cookie = oai_sc_val  # None clears an existing value

        # Propagate to token_cache so collectors can find it immediately.
        # Stamp under the resolved account_id so dashboard-saved credentials
        # and the collector's resolved identity stay aligned in the cache.
        if row.session_cookie and provider_id != "opencode":
            # Map generic session_cookie to all common provider-specific keys
            # to ensure the manual override works across various collector implementations.
            tokens = {
                "session_cookie": row.session_cookie,
                "cookie_session": row.session_cookie,
                "cookie_sessionKey": row.session_cookie,
                "cookie___Secure-next-auth.session-token": row.session_cookie,
            }
            if provider_id == "chatgpt" and oai_sc_val:
                tokens["cookie_oai-sc"] = oai_sc_val

            await token_cache.store(provider_id, tokens, account_id=account_id, source="config")

    session.commit()
    # Invalidate server-side fleet/limits caches so archived/restored
    # providers appear or disappear immediately on the next dashboard poll.
    from app.core.cache import cache_clear

    cache_clear()
    # Trigger immediate sync and collection to reflect changes in dashboard
    # instantly. `force=True` bypasses the 60s throttle so a disable takes
    # effect now; `collect_one` is skipped when no collector remains for
    # this provider (every account disabled / archived) so we don't refresh
    # cards for a provider the user just turned off.
    try:
        await manager._sync_collectors(force=True)
        if any(key.startswith(f"{provider_id}:") for key in manager.smart_collectors):
            await manager.collect_one(provider_id)
    except Exception as e:
        logger.warning(
            f"Failed to trigger sync after config update for {scrub_log(provider_id)}: {e}"
        )

    # Wake poller so a per-provider interval change applies on the next tick.
    from app.services.poller import poller

    poller.wake()


@router.get("/app-config")
@limiter.limit("30/minute")
async def get_app_config(request: Request, session: Session = Depends(get_session)) -> dict:
    """Return global application configuration."""
    cfg = session.exec(select(SystemConfig)).first()
    return {
        "browser_preference": (cfg.browser_preference if cfg else None)
        or settings.BROWSER_PREFERENCE,
        "default_poll_interval_seconds": cfg.default_poll_interval_seconds if cfg else None,
        "user_timezone": cfg.user_timezone if cfg else None,
        "sidecar_update_channel": (cfg.sidecar_update_channel if cfg else None) or "stable",
        "sidecar_auto_update": bool(cfg.sidecar_auto_update) if cfg else False,
        "env_timezone": settings.env_timezone,
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
    if body.user_timezone is not None:
        if body.user_timezone == "":
            cfg.user_timezone = None
        else:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(body.user_timezone)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid IANA timezone: {body.user_timezone!r}",
                ) from e
            cfg.user_timezone = body.user_timezone
        # resolve_user_tz() and every period-boundary-dependent response
        # (/fleet, /global-stats, /top-*, /forecast) cache their output — a
        # tz change must take effect immediately, not wait out the TTL.
        from app.core.cache import cache_clear

        cache_clear()
    if body.sidecar_update_channel is not None:
        channel = body.sidecar_update_channel.strip().lower()
        if channel in ("", "stable"):
            cfg.sidecar_update_channel = None
        elif channel == "edge":
            cfg.sidecar_update_channel = "edge"
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid update channel: {body.sidecar_update_channel!r} (expected 'stable' or 'edge')",
            )
    if body.sidecar_auto_update is not None:
        cfg.sidecar_auto_update = bool(body.sidecar_auto_update)
    session.commit()

    # Wake poller so the new interval applies on the next tick rather than
    # waiting out the current sleep.
    from app.services.poller import poller

    poller.wake()
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
