import hashlib
import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.core.utils import scrub_log
from app.models.db import LatestUsage, ProviderConfig, SidecarRegistry, SystemConfig
from app.models.schemas import IngestRequest
from app.services import audit_log
from app.services.account_identity import resolve_account_id
from app.services.accumulator import prune_stale_latest_usage, upsert_latest_usage
from app.services.fleet_registry import fleet_registry
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)
router = APIRouter()


class SidecarUpdateRequest(BaseModel):
    custom_name: str | None = None
    tags: list[str] | None = None


@router.post("/ingest")
@limiter.limit("600/minute")
async def ingest_metrics(  # noqa: PLR0915 — known-debt: end-to-end ingest entrypoint, refactor tracked separately
    request: Request,
    x_signature: str = Header(None, alias="X-Signature"),
    x_timestamp: str = Header(None, alias="X-Timestamp"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """
    Ingest metrics from sidecar with HMAC-SHA256 signature verification.

    Headers required:
    - X-Signature: HMAC-SHA256(secret, timestamp + body)
    - X-Timestamp: Unix timestamp (within 5 minutes)

    Rate limit: 600 requests / minute per source IP. Sidecars batch up to
    1000 events per push at a 15-min cadence (spec §7.3), so even a fleet
    of 100 sidecars stays well under 5 req/min. The limit's there to keep
    a flooding attacker from saturating the HMAC + Pydantic parse path,
    not to throttle legitimate operators.
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
    body_bytes = await request.body()
    # 8 MB cap. Sidecars batch large event backfills (spec §7.3: ≤1000 events/POST).
    if len(body_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Request body too large")
    expected_sig = hmac.new(
        settings.INGEST_API_KEY.encode(),
        f"{x_timestamp}".encode() + body_bytes,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(x_signature, expected_sig):
        logger.warning(
            f"HMAC mismatch. Received: {scrub_log(x_signature[:8])}... (len: {len(x_signature)})"
        )
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    # 4. Parse request
    try:
        payload = IngestRequest.model_validate_json(body_bytes)
    except Exception as e:
        logger.error(f"Failed to parse ingest payload: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid payload: {str(e)}")

    tokens_to_store = []
    local_cards = []

    for card in payload.metrics:
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
                            key in ("oauth_token", "refresh_token", "api_key", "id_token")
                            or key.startswith("cookie_")
                        ):
                            provider_tokens[key] = val

                if provider_tokens:
                    tokens_to_store.append((provider_id, provider_tokens, acc_id, acc_label))
                    logger.debug(
                        f"Extracted {list(provider_tokens.keys())} for {provider_id} account {acc_id or 'auto'} from {payload.provider}"
                    )
            continue

        # Propagate sidecar_id from the request to each card (if not already set)
        if payload.sidecar_id and not card.sidecar_id:
            card.sidecar_id = payload.sidecar_id

        # Keep actual data cards
        local_cards.append(card)

    # Register/update sidecar in persistent fleet registry (non-fatal)
    if payload.sidecar_id:
        source_ip = request.client.host if request.client else "unknown"
        try:
            fleet_registry.upsert_sidecar(
                payload.sidecar_id,
                source_ip,
                session,
                sidecar_version=payload.sidecar_version,
                os_platform=payload.os_platform,
                collection_errors=payload.collection_errors,
                last_log_lines=payload.last_log_lines or [],
            )
        except Exception as _e:
            logger.warning(f"Fleet registry upsert failed for '{payload.sidecar_id}': {_e}")

    # Store tokens in cache for each identified account
    tokens_received_count = 0
    for p_id, p_tokens, a_id, a_name in tokens_to_store:
        actual_acc_id = await token_cache.store(
            p_id, p_tokens, a_id, a_name, source=payload.sidecar_id
        )
        tokens_received_count += len(p_tokens)
        logger.info(
            f"Received {len(p_tokens)} tokens for {p_id} account {actual_acc_id} from {payload.provider}"
        )

    # Store local data cards directly into LatestUsage (unified with server-scraped cards)
    if local_cards:
        # Track (provider_id, canonical_account_id) → set of
        # (window_type, variant, model_id) for the prune step.
        batch_keys: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
        for card in local_cards:
            card_dict = card.model_dump(exclude_none=True)
            upsert_latest_usage(
                session,
                card_dict,
                sidecar_id_override=card.sidecar_id or payload.sidecar_id or "local",
            )
            if card.provider_id and card.account_id:
                canonical_aid = resolve_account_id(
                    card.provider_id, card.account_id, card.account_label
                )
                batch_keys.setdefault((card.provider_id, canonical_aid), set()).add(
                    (
                        card.window_type or "",
                        card.variant or "default",
                        card.model_id or "",
                    )
                )
        pruned = prune_stale_latest_usage(session, batch_keys)
        session.commit()
        logger.info(
            f"Stored {len(local_cards)} local cards into LatestUsage from {payload.provider}"
            + (f" (pruned {pruned} ghost row(s))" if pruned else "")
        )

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
    if payload.events:
        from app.services.event_ingestor import EventIngestor

        try:
            ingestor = EventIngestor(session)
            ingest_result = ingestor.ingest(payload.events, sidecar_id=payload.sidecar_id)
            logger.info(
                f"Ingested {ingest_result.events_inserted} events "
                f"({ingest_result.events_duplicate} dup) from {payload.sidecar_id or 'unknown'}"
            )
        except Exception as e:
            logger.error(f"Event ingestion failed: {e}")
            ingest_result = None

    # Determine which providers this sidecar should poll right now.
    # The server is the cadence authority — sidecars heartbeat frequently and
    # collect only what we tell them via poll_providers (per-provider intervals
    # gate this server-side via fleet_registry.get_due_providers).
    poll_providers: list[str] = []
    trigger: bool = False
    sys_cfg = session.exec(select(SystemConfig)).first()
    collection_enabled = True
    if payload.sidecar_id:
        # Honor per-sidecar pause: paused sidecars still check in but receive
        # no poll instructions, and their pending-trigger flag is preserved
        # so a resume can still deliver it.
        sc_row = session.get(SidecarRegistry, payload.sidecar_id)
        if sc_row is not None and not sc_row.collection_enabled:
            collection_enabled = False
        else:
            global_interval = (sys_cfg.default_poll_interval_seconds if sys_cfg else None) or 900

            enabled_provider_rows = session.exec(
                select(ProviderConfig).where(ProviderConfig.enabled)
            ).all()
            configured = {row.provider_id for row in enabled_provider_rows}

            # Passive providers (antigravity, opencode-free, …) have no
            # provider_configs row because they need no credentials — the
            # sidecar discovers them locally. Without this they'd never
            # appear in poll_providers and would only refresh on cold-start
            # or a user-triggered full refresh.
            passive_pids = (
                set(session.exec(select(LatestUsage.provider_id).distinct()).all()) - configured
            )

            provider_intervals = [
                (row.provider_id, row.poll_interval_seconds or global_interval)
                for row in enabled_provider_rows
            ] + [(pid, global_interval) for pid in sorted(passive_pids)]

            poll_providers, trigger = fleet_registry.get_due_providers(
                payload.sidecar_id, provider_intervals
            )
            if poll_providers:
                logger.info(f"Instructing sidecar '{payload.sidecar_id}' to poll: {poll_providers}")

    return {
        "status": "ok",
        "provider": payload.provider,
        "tokens_received": tokens_received_count,
        "metrics_stored": len(local_cards),
        "events_received": ingest_result.events_received if ingest_result else 0,
        "events_inserted": ingest_result.events_inserted if ingest_result else 0,
        "events_duplicate": ingest_result.events_duplicate if ingest_result else 0,
        "windows_closed": ingest_result.windows_closed if ingest_result else 0,
        "poll_providers": poll_providers,
        "trigger": trigger,
        "collection_enabled": collection_enabled,
        "identities": _get_active_identities(session),  # For sidecar identity propagation
        "reset_anchors": _reset_anchors_for_sidecar(session),  # Phase 6
        # Update channel the sidecar should track for its "update available"
        # check ("stable" | "edge"). The dashboard owns this setting.
        "sidecar_update_channel": (sys_cfg.sidecar_update_channel if sys_cfg else None) or "stable",
    }


def _get_active_identities(session: Session) -> dict[str, str]:
    """Map provider_id to the most recent 'real' account_id seen in LatestUsage.

    Used by sidecars to discover their identity when local logs are anonymous.
    """
    from app.models.db import LatestUsage

    rows = session.exec(
        select(LatestUsage.provider_id, LatestUsage.account_id)
        .where(LatestUsage.account_id != "default")
        .where(col(LatestUsage.account_id).is_not(None))
        .order_by(col(LatestUsage.updated_at).desc())
    ).all()

    identities = {}
    for pid, aid in rows:
        if pid not in identities:
            identities[pid] = aid
    return identities


def _reset_anchors_for_sidecar(session: Session) -> dict[str, dict[str, str]]:
    """Per-provider authoritative reset_at by window_type, for sidecar use.

    Reads all LatestUsage rows with future reset_at and builds a dict of
    the latest reset_at per (provider_id, window_type) pair. Filters to
    only default variants (model_id="" and variant in ("", "default")).

    Returns:
        {
          "anthropic": {
            "session": "2026-05-08T18:00:00+00:00",
            "weekly":  "2026-05-12T18:00:00+00:00"
          },
          ...
        }
    """
    import json
    from datetime import UTC, datetime

    from app.models.db import LatestUsage

    # Push the default-variant / no-model-id filters into SQL so we don't
    # materialise per-model rows that we'd immediately discard.
    rows = session.exec(
        select(LatestUsage).where(
            LatestUsage.model_id == "",
            col(LatestUsage.variant).in_(["", "default"]),
        )
    ).all()
    now = datetime.now(UTC)
    anchors: dict[str, dict[str, str]] = {}

    for r in rows:
        # Defense-in-depth: filters above are already SQL-enforced.
        if r.model_id and r.model_id != "":
            continue
        if r.variant not in ("", "default"):
            continue

        # Parse card_json
        try:
            card = json.loads(r.card_json) if r.card_json else {}
        except json.JSONDecodeError:
            continue

        # Extract reset_at
        reset_at = card.get("reset_at")
        if not reset_at:
            continue

        # Parse datetime and check if it's in the future
        try:
            reset_dt = parse_iso8601_utc(reset_at)
        except ValueError:
            continue

        if reset_dt <= now:
            continue

        # Track the latest reset_at per (provider_id, window_type)
        prov_anchors = anchors.setdefault(r.provider_id, {})
        existing = prov_anchors.get(r.window_type)
        if existing is None or reset_at > existing:
            prov_anchors[r.window_type] = reset_at

    return anchors


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
    audit_log.record(
        session,
        request,
        action="sidecar.update",
        target_id=sidecar_id,
        payload={"custom_name": body.custom_name, "tags": body.tags},
    )
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
    audit_log.record(session, request, action="sidecar.delete", target_id=sidecar_id)
    return {"status": "deleted", "sidecar_id": sidecar_id}


def _set_sidecar_collection_enabled(
    sidecar_id: str, enabled: bool, session: Session
) -> SidecarRegistry:
    row = session.get(SidecarRegistry, sidecar_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    row.collection_enabled = enabled
    session.commit()
    session.refresh(row)
    logger.info(f"Sidecar '{scrub_log(sidecar_id)}' collection_enabled set to {enabled}")
    return row


@router.post("/sidecars/{sidecar_id}/pause")
@limiter.limit("10/minute")
async def pause_sidecar(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Pause collection on the named sidecar. The sidecar continues to check
    in but receives no poll instructions until resumed."""
    _set_sidecar_collection_enabled(sidecar_id, False, session)
    audit_log.record(session, request, action="sidecar.pause", target_id=sidecar_id)
    return {"status": "paused", "sidecar_id": sidecar_id, "collection_enabled": False}


@router.post("/sidecars/{sidecar_id}/resume")
@limiter.limit("10/minute")
async def resume_sidecar(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Resume collection on the named sidecar."""
    _set_sidecar_collection_enabled(sidecar_id, True, session)
    audit_log.record(session, request, action="sidecar.resume", target_id=sidecar_id)
    return {"status": "resumed", "sidecar_id": sidecar_id, "collection_enabled": True}


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
