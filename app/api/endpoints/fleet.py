import hashlib
import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.models.db import ProviderConfig, SidecarRegistry, SystemConfig
from app.models.schemas import IngestRequest
from app.services.external_metrics import external_metric_service
from app.services.fleet_registry import fleet_registry
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)
router = APIRouter()


class SidecarUpdateRequest(BaseModel):
    custom_name: str | None = None
    tags: list[str] | None = None


@router.post("/ingest")
async def ingest_metrics(
    raw_request: Request,
    x_signature: str = Header(None, alias="X-Signature"),
    x_timestamp: str = Header(None, alias="X-Timestamp"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """
    Ingest metrics from sidecar with HMAC-SHA256 signature verification.

    Headers required:
    - X-Signature: HMAC-SHA256(secret, timestamp + body)
    - X-Timestamp: Unix timestamp (within 5 minutes)
    """
    # 0. Guard against misconfigured or insecure API key
    if not settings.INGEST_API_KEY:
        logger.error("INGEST_API_KEY is empty — ingest endpoint is disabled")
        raise HTTPException(
            status_code=503,
            detail="Ingest endpoint not configured: INGEST_API_KEY is empty",
        )
    if settings.INGEST_API_KEY_IS_INSECURE_DEFAULT:
        logger.error("INGEST_API_KEY is the default insecure value — ingest endpoint is disabled")
        raise HTTPException(
            status_code=503,
            detail="Ingest endpoint not configured: INGEST_API_KEY must be changed from default",
        )

    # 1. Check headers
    if not x_signature or not x_timestamp:
        logger.warning("Ingest attempt with missing HMAC headers")
        raise HTTPException(status_code=401, detail="Missing HMAC signature or timestamp")

    # 2. Check timestamp (5-minute window for past, 60s for future drift)
    try:
        ts = float(x_timestamp)
        now = time.time()
        skew = now - ts
        if skew < -60 or skew > 300:
            logger.warning(f"Ingest attempt with rejected timestamp: {skew:.0f}s difference")
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "timestamp_expired" if skew > 0 else "timestamp_future",
                    "skew_seconds": round(skew, 1),
                    "message": "Clock skew detected. Please check NTP sync on the sidecar machine.",
                },
            )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid X-Timestamp format")

    # 3. Read body and verify signature
    body_bytes = await raw_request.body()
    if len(body_bytes) > 1 * 1024 * 1024:  # 1 MB sanity limit
        raise HTTPException(status_code=413, detail="Request body too large")
    expected_sig = hmac.new(
        settings.INGEST_API_KEY.encode(),
        f"{x_timestamp}".encode() + body_bytes,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(x_signature, expected_sig):
        logger.warning(f"HMAC mismatch. Received: {x_signature[:8]}... (len: {len(x_signature)})")
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    # 4. Parse request
    try:
        request = IngestRequest.model_validate_json(body_bytes)
    except Exception as e:
        logger.error(f"Failed to parse ingest payload: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid payload: {str(e)}")

    tokens_to_store = []
    local_cards = []

    for card in request.metrics:
        # Check if this is a token-only card (should NOT be displayed)
        is_token_only = card.remaining == "Token" and card.unit in ("oauth", "api_key", "cookie")

        if is_token_only:
            # Extract provider/account identifiers: prefer top-level fields, fall back to metadata
            provider_id = card.provider_id or (
                card.metadata.get("provider_id") if card.metadata else None
            )
            if provider_id:
                acc_id = card.account_id or (
                    card.metadata.get("account_id") if card.metadata else None
                )
                acc_label = card.account_label or (
                    card.metadata.get("account_label") if card.metadata else None
                )

                provider_tokens = {}
                if card.metadata:
                    for key, val in card.metadata.items():
                        # Store tokens but skip the provider/account identifiers
                        if key not in (
                            "provider_id",
                            "account_id",
                            "account_label",
                        ) and (
                            key in ("oauth_token", "refresh_token", "api_key")
                            or key.startswith("cookie_")
                        ):
                            provider_tokens[key] = val

                if provider_tokens:
                    tokens_to_store.append((provider_id, provider_tokens, acc_id, acc_label))
                    logger.debug(
                        f"Extracted {list(provider_tokens.keys())} for {provider_id} account {acc_id or 'auto'} from {request.provider}"
                    )
            continue

        # Propagate sidecar_id from the request to each card (if not already set)
        if request.sidecar_id and not card.sidecar_id:
            card.sidecar_id = request.sidecar_id

        # Keep actual data cards
        local_cards.append(card)

    # Register/update sidecar in persistent fleet registry (non-fatal)
    if request.sidecar_id:
        source_ip = raw_request.client.host if raw_request.client else "unknown"
        try:
            fleet_registry.upsert_sidecar(
                request.sidecar_id,
                source_ip,
                session,
                sidecar_version=request.sidecar_version,
                os_platform=request.os_platform,
                collection_errors=request.collection_errors,
                last_log_lines=request.last_log_lines or [],
            )
        except Exception as _e:
            logger.warning(f"Fleet registry upsert failed for '{request.sidecar_id}': {_e}")

    # Store tokens in cache for each identified account
    tokens_received_count = 0
    for p_id, p_tokens, a_id, a_name in tokens_to_store:
        actual_acc_id = await token_cache.store(
            p_id, p_tokens, a_id, a_name, source=request.sidecar_id
        )
        tokens_received_count += len(p_tokens)
        logger.info(
            f"Received {len(p_tokens)} tokens for {p_id} account {actual_acc_id} from {request.provider}"
        )

    # Store local data metrics
    if local_cards:
        await external_metric_service.metrics_update_from_ingest(request.provider, local_cards)
        logger.info(f"Stored {len(local_cards)} metrics from {request.provider}")

    # Wake the poller whenever the sidecar pushes anything actionable —
    # tokens or local cards. Without this, token-only payloads (the common
    # case) leave the poller asleep until its 15-min interval, so the
    # dashboard stays empty even though credentials are in the cache.
    if tokens_to_store or local_cards:
        from app.services.collector_manager import manager
        from app.services.poller import poller

        # Force the next collect_all to re-sync per-account collectors so
        # the freshly-pushed accounts get SmartCollectors immediately
        # instead of waiting the 60s sync throttle.
        manager._last_sync_time = 0.0
        poller.wake()

    # Process events for atomic usage tracking
    ingest_result = None
    if request.events:
        from app.services.event_ingestor import EventIngestor

        try:
            ingestor = EventIngestor(session)
            ingest_result = ingestor.ingest(request.events, sidecar_id=request.sidecar_id)
            logger.info(
                f"Ingested {ingest_result.events_inserted} events "
                f"({ingest_result.events_duplicate} dup) from {request.sidecar_id or 'unknown'}"
            )
        except Exception as e:
            logger.error(f"Event ingestion failed: {e}")
            ingest_result = None

    # Determine which providers this sidecar should poll right now.
    # Logic: Centralized orchestration based on UI-configured intervals.
    poll_providers: list[str] = []
    trigger: bool = False
    if request.sidecar_id:
        # Fetch configurations to determine intervals
        sys_cfg = session.exec(select(SystemConfig)).first()
        global_interval = (
            sys_cfg.default_poll_interval_seconds if sys_cfg else None
        ) or 1800  # Fallback to 30 mins

        enabled_provider_rows = session.exec(
            select(ProviderConfig).where(ProviderConfig.enabled)
        ).all()

        provider_intervals = [
            (row.provider_id, row.poll_interval_seconds or global_interval)
            for row in enabled_provider_rows
        ]

        poll_providers, trigger = fleet_registry.get_due_providers(
            request.sidecar_id, provider_intervals
        )
        if poll_providers:
            logger.info(f"Instructing sidecar '{request.sidecar_id}' to poll: {poll_providers}")

    return {
        "status": "ok",
        "provider": request.provider,
        "tokens_received": tokens_received_count,
        "metrics_stored": len(local_cards),
        "events_received": ingest_result.events_received if ingest_result else 0,
        "events_inserted": ingest_result.events_inserted if ingest_result else 0,
        "events_duplicate": ingest_result.events_duplicate if ingest_result else 0,
        "windows_closed": ingest_result.windows_closed if ingest_result else 0,
        "poll_providers": poll_providers,
        "trigger": trigger,
        "reset_anchors": _reset_anchors_for_sidecar(session),  # Phase 6
    }


def _reset_anchors_for_sidecar(session: Session) -> dict:  # noqa: ARG001
    return {}  # Phase 6 wires this to authoritative scrape data


@router.get("/sidecars")
@limiter.limit("30/minute")
async def list_sidecars(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """List all registered sidecars."""
    rows = session.exec(
        select(SidecarRegistry).order_by(col(SidecarRegistry.last_seen).desc())
    ).all()
    return {"sidecars": [fleet_registry.to_dict(row) for row in rows]}


@router.get("/sidecars/{sidecar_id}")
@limiter.limit("30/minute")
async def get_sidecar(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Get a single sidecar by ID."""
    row = session.get(SidecarRegistry, sidecar_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    return fleet_registry.to_dict(row)


@router.patch("/sidecars/{sidecar_id}")
@limiter.limit("30/minute")
async def update_sidecar(
    request: Request,
    sidecar_id: str,
    body: SidecarUpdateRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Update custom_name and/or tags for a sidecar."""
    row = fleet_registry.update_sidecar(sidecar_id, body.custom_name, body.tags, session)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    return fleet_registry.to_dict(row)


@router.delete("/sidecars/{sidecar_id}")
@limiter.limit("30/minute")
async def delete_sidecar(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Remove a sidecar from the registry."""
    deleted = fleet_registry.delete_sidecar(sidecar_id, session)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    return {"status": "deleted", "sidecar_id": sidecar_id}


@router.post("/sidecars/{sidecar_id}/trigger")
@limiter.limit("10/minute")
async def trigger_sidecar_collect(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Request an immediate collection cycle on the named sidecar.

    The trigger is delivered the next time the sidecar posts an ingest request.
    Returns 404 if the sidecar is not registered.
    """
    row = session.get(SidecarRegistry, sidecar_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    fleet_registry.set_pending_trigger(sidecar_id)
    return {"status": "trigger_queued", "sidecar_id": sidecar_id}


@router.get("/config")
@limiter.limit("60/minute")
async def get_fleet_config(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Retrieve active collection configuration for sidecars.

    This endpoint does not require the admin key (as sidecars do not have it)
    but relies on rate limiting. It returns only the logical state (enabled/disabled
    providers and strategies), no sensitive keys or tokens.
    """
    from app.models.db import ProviderConfig

    rows = session.exec(select(ProviderConfig)).all()

    config: dict[str, dict] = {"providers": {}}

    for row in rows:
        # We only care about global defaults for sidecars, or merge all account settings
        # To keep it simple, if *any* account has a provider enabled, the sidecar collects it.
        if row.provider_id not in config["providers"]:
            config["providers"][row.provider_id] = {
                "enabled": row.enabled,
                "strategies": row.strategies,
            }
        # If we already have it, but this row is enabled, we mark it enabled overall
        elif row.enabled:
            config["providers"][row.provider_id]["enabled"] = True

        # Merge strategies if present
        if row.strategies and row.enabled:
            config["providers"][row.provider_id]["strategies"] = row.strategies

    from app.services.collector_manager import collector_manager

    # Ensure all registered providers have a default entry if not in DB
    for p_id in collector_manager.collector_registry:
        if p_id not in config["providers"]:
            config["providers"][p_id] = {"enabled": True, "strategies": None}

    return {"status": "ok", "config": config}
