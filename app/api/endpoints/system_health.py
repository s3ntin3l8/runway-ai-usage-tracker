import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete
from sqlmodel import Session, col, select

from app import __version__
from app.core.build_info import get_build_sha
from app.core.config import settings
from app.core.db import get_session
from app.core.encryption import encryption_service
from app.core.log_redaction import redact_secrets, redact_url
from app.core.rate_limit import limiter
from app.core.security import SESSION_COOKIE, require_admin_key, resolve_auth
from app.core.utils import scrub_log
from app.models._datetime import iso_utc
from app.models.db import (
    AuditLog,
    ClaudeCloudCollectorState,
    LatestUsage,
    SidecarRegistry,
)
from app.models.schemas import SidecarDownloadsResponse
from app.services import audit_log
from app.services.collector_manager import manager
from app.services.credential_provider import CredentialProvider
from app.services.sidecar_downloads import sidecar_downloads
from app.services.sidecar_version_checker import is_update_available, sidecar_version_checker
from app.services.source_probe import isolated_collector, probe_sources
from app.services.token_health import token_health_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with known secret values masked."""
    safe = dict(headers)
    for key in safe:
        if key.lower() in _SECRET_HEADERS:
            safe[key] = "[MASKED]"
    return safe


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Mask known secret headers, then scrub token shapes in the remaining values."""
    return {k: str(redact_secrets(v)) for k, v in _mask_headers(headers).items()}


def _capture_request_entry(r: httpx.Request) -> dict[str, Any]:
    return {
        "method": r.method,
        "url": redact_url(str(r.url)),
        "headers": _redact_headers(dict(r.headers)),
        "timestamp": time.time(),
    }


def _capture_response_entry(r: httpx.Response) -> dict[str, Any]:
    try:
        data: Any = r.json()
    except Exception:
        data = r.text
    return {
        "url": redact_url(str(r.url)),
        "method": r.request.method,
        "status": r.status_code,
        "headers": _redact_headers(dict(r.headers)),
        "body": redact_secrets(data),
        "timestamp": time.time(),
    }


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
        reqs.append(_capture_request_entry(r))

    async def _capture_response(r: httpx.Response) -> None:
        await r.aread()
        resps.append(_capture_response_entry(r))

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
            errs.append({"type": type(exc).__name__, "message": str(redact_secrets(str(exc)))})

    is_err = not card_results or any(r.get("remaining") == "ERR" for r in card_results)

    return {
        "label": _debug_strategy_label(collector.STRATEGIES, s_id),
        "kind": kind,
        "status": "error" if errs else ("error" if is_err else "success"),
        "cards_returned": len(card_results),
        "cards_summary": [
            {
                "service_name": c.get("service_name"),
                "remaining": c.get("remaining"),
                "error_type": c.get("error_type"),
                "detail": str(redact_secrets(c.get("detail", ""))),
            }
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
        # The commit this build came from. `version` names a release and is
        # identical across every `:edge` image cut from main; the SHA is what
        # identifies one exact build. None when the build carries no SHA.
        "build_sha": get_build_sha(),
        "latest_version": latest,
        "update_available": is_update_available(__version__, latest, target_channel="stable"),
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


_CLOUD_UNHEALTHY_FAILURES = 3


def _claude_cloud_health(session: Session) -> dict[str, Any]:
    enabled = settings.CLAUDE_CLOUD_COLLECTOR_ENABLED
    accounts: list[dict[str, Any]] = []
    if enabled:
        try:
            rows = session.exec(
                select(ClaudeCloudCollectorState).order_by(
                    col(ClaudeCloudCollectorState.account_id)
                )
            ).all()
        except Exception as e:
            logger.warning("Claude cloud health read failed (%s)", type(e).__name__)
            rows = []
        stale_after = 3 * settings.CLAUDE_CLOUD_COLLECTOR_INTERVAL_SECONDS
        now = time.time()
        for row in rows:
            healthy = (
                row.consecutive_failures < _CLOUD_UNHEALTHY_FAILURES
                and now - row.last_cycle_at <= stale_after
            )
            accounts.append(
                {
                    "account_id": row.account_id,
                    "last_outcome": row.last_outcome,
                    "last_cycle_at": row.last_cycle_at,
                    "last_events": row.last_events,
                    "consecutive_failures": row.consecutive_failures,
                    "backlog": bool(row.backlog),
                    "healthy": healthy,
                }
            )
    return {"enabled": enabled, "accounts": accounts}


@router.get("/status", dependencies=[Depends(require_admin_key)])
@limiter.limit("30/minute")
async def get_collector_status(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Return detailed health and cache stats for all active collectors.

    ``claude_cloud`` carries the cloud-runner collector's per-account health record: numbers,
    enums and the account id only.
    """
    try:
        await manager._sync_collectors()
    except Exception as e:
        logger.error(f"Failed to sync collectors for status: {e}")
    return {**manager.get_collector_stats(), "claude_cloud": _claude_cloud_health(session)}


@router.get("/sidecar-downloads", response_model=SidecarDownloadsResponse)
@limiter.limit("30/minute")
async def get_sidecar_downloads(
    request: Request, channel: Literal["stable", "beta", "edge"] = "stable"
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
        "update_available": is_update_available(__version__, latest, target_channel="stable"),
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


@router.post("/debug/sources/{provider_id}")
@limiter.limit("5/minute")
async def probe_provider_sources(
    request: Request,
    provider_id: str,
    account_id: str = "default",
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Try each credential source of one account once, live, and report how each fares.

    Admin-gated and rate-limited: it makes a real upstream request per source. Writes nothing
    (no health, no promotion, no backoff) and never refreshes a token. Secrets are redacted;
    only status codes, error types and counts are returned.
    """
    if provider_id not in manager.collector_registry:
        raise HTTPException(
            status_code=404, detail=f"No collector found for provider: {provider_id}"
        )
    sources = await probe_sources(manager, provider_id, account_id)
    return {
        "provider_id": provider_id,
        "account_id": account_id,
        "probed_at": datetime.now(UTC).isoformat(),
        "truncated": any(source["outcome"] == "over_limit" for source in sources),
        "sources": sources,
    }


@router.get("/debug/raw/{provider_id}")
@limiter.limit("10/minute")
async def get_raw_provider_data(
    request: Request,
    provider_id: str,
    account_id: str | None = None,
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """
    Run a specific collector and capture raw HTTP responses.
    Useful for troubleshooting provider API changes.
    Admin-gated: returns live upstream URLs/bodies (secrets redacted).
    """
    raw_requests: list[dict[str, Any]] = []
    raw_responses: list[dict[str, Any]] = []
    collect_errors: list[dict[str, Any]] = []

    async def intercept_request(request: httpx.Request) -> None:
        raw_requests.append(_capture_request_entry(request))

    async def intercept_response(response: httpx.Response) -> None:
        await response.aread()
        raw_responses.append(_capture_response_entry(response))

    try:
        await manager._sync_collectors()

        if account_id is not None:
            selected = manager.smart_collectors.get(f"{provider_id}:{account_id}")
            if selected is None:
                account_matches = [
                    sc
                    for key, sc in manager.smart_collectors.items()
                    if (key.startswith(f"{provider_id}:") or key == provider_id)
                    and getattr(sc.collector, "account_id", None) == account_id
                ]
                if len(account_matches) == 1:
                    selected = account_matches[0]
            target_collectors = [selected.collector] if selected else []
        else:
            target_collectors = [
                sc.collector
                for key, sc in manager.smart_collectors.items()
                if key.startswith(f"{provider_id}:") or key == provider_id
            ]

        if not target_collectors and account_id is None:
            collector = manager._create_collector(provider_id)
            if collector:
                target_collectors = [collector]

        if not target_collectors:
            raise HTTPException(
                status_code=404, detail=f"No collector found for provider: {provider_id}"
            )

        # Run on a copy: the poller's own collector holds state mid-collection (and this
        # capture resets it), so a capture must never share an instance with a poll.
        collector = isolated_collector(target_collectors[0])
        is_configured = await collector.is_configured()

        creds = CredentialProvider.get_credentials(provider_id, account_id=account_id)
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
                    collect_errors.append(
                        {"type": type(exc).__name__, "message": str(redact_secrets(str(exc)))}
                    )
            # Fold legacy collector data into a synthetic strategy entry so
            # the response shape is consistent with the per-strategy path.
            strategy_results["_legacy"] = {
                "label": f"{provider_id} (legacy)",
                "kind": "primary",
                "status": "error" if collect_errors else "success",
                "cards_returned": active_strategy_card_count,
                "cards_summary": [
                    {
                        "service_name": c.get("service_name"),
                        "remaining": c.get("remaining"),
                        "error_type": c.get("error_type"),
                        "detail": str(redact_secrets(c.get("detail", ""))),
                    }
                    for c in (result or [])[:5]
                ],
                "requests": raw_requests,
                "responses": raw_responses,
                "errors": collect_errors,
            }
            return {
                "provider_id": provider_id,
                "account_id": account_id,
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
            "account_id": account_id,
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
        safe_message = str(redact_secrets(str(e)))
        logger.error(f"Raw debug collection failed for {scrub_log(provider_id)}: {safe_message}")
        raise HTTPException(status_code=500, detail=safe_message)


__all__ = [
    "CleanupRequest",
    "_capture_request_entry",
    "_capture_response_entry",
    "_debug_run_one_strategy",
    "_debug_split_strategies",
    "_debug_strategy_label",
    "_mask_headers",
    "_redact_headers",
    "check_updates",
    "cleanup_database",
    "force_collect",
    "get_app_settings",
    "get_audit_log",
    "get_collector_status",
    "get_raw_provider_data",
    "get_sidecar_downloads",
    "get_token_health",
    "health_check",
    "probe_provider_sources",
    "wake_poller",
]
_SECRET_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "x-api-key",
        "x-auth-token",
        "x-amz-security-token",
        "x-session-token",
        "x-iam-token",
        "x-activity-session-id",
        "set-cookie",
    }
)
