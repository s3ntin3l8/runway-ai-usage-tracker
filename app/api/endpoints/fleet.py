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
from app.models.db import SidecarRegistry
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
        is_token_only = (
            card.remaining == "Token"
            and card.unit in ("oauth", "api_key")
            and card.data_source == "token_extracted"
        )

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
            fleet_registry.upsert_sidecar(request.sidecar_id, source_ip, session)
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

    return {
        "status": "ok",
        "provider": request.provider,
        "tokens_received": tokens_received_count,
        "metrics_stored": len(local_cards),
    }


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
