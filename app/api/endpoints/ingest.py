from fastapi import APIRouter, HTTPException, Header, Request
from datetime import datetime, timezone
from typing import Dict, Optional, List, Any
import hmac
import hashlib
import time
import logging
from app.models.schemas import IngestRequest, LimitCard
from app.services.external_metrics import external_metric_service
from app.services.token_cache import token_cache
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/ingest")
async def ingest_metrics(
    raw_request: Request,
    x_signature: str = Header(None, alias="X-Signature"),
    x_timestamp: str = Header(None, alias="X-Timestamp"),
) -> Dict[str, Any]:
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
        logger.error(
            "INGEST_API_KEY is the default insecure value — ingest endpoint is disabled"
        )
        raise HTTPException(
            status_code=503,
            detail="Ingest endpoint not configured: INGEST_API_KEY must be changed from default",
        )

    # 1. Check headers
    if not x_signature or not x_timestamp:
        logger.warning("Ingest attempt with missing HMAC headers")
        raise HTTPException(
            status_code=401, detail="Missing HMAC signature or timestamp"
        )

    # 2. Check timestamp (5-minute window for past, 60s for future drift)
    try:
        ts = float(x_timestamp)
        now = time.time()
        skew = now - ts
        if skew < -60 or skew > 300:
            logger.warning(
                f"Ingest attempt with rejected timestamp: {skew:.0f}s difference"
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "timestamp_expired" if skew > 0 else "timestamp_future",
                    "skew_seconds": round(skew, 1),
                    "message": "Clock skew detected. Please check NTP sync on the sidecar machine."
                }
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
        logger.warning(
            f"HMAC mismatch. Received: {x_signature[:8]}... (len: {len(x_signature)})"
        )
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
            provider_id = card.provider_id or (card.metadata.get("provider_id") if card.metadata else None)
            if provider_id:
                acc_id = card.account_id or (card.metadata.get("account_id") if card.metadata else None)
                acc_label = card.account_label or (card.metadata.get("account_label") if card.metadata else None)

                provider_tokens = {}
                if card.metadata:
                    for key, val in card.metadata.items():
                        # Store tokens but skip the provider/account identifiers
                        if key not in ("provider_id", "account_id", "account_label") and (
                            key in ("oauth_token", "refresh_token", "api_key")
                            or key.startswith("cookie_")
                        ):
                            provider_tokens[key] = val

                if provider_tokens:
                    tokens_to_store.append((provider_id, provider_tokens, acc_id, acc_label))
                    logger.debug(f"Extracted {list(provider_tokens.keys())} for {provider_id} account {acc_id or 'auto'} from {request.provider}")
            continue

        # Propagate sidecar_id from the request to each card (if not already set)
        if request.sidecar_id and not card.sidecar_id:
            card.sidecar_id = request.sidecar_id

        # Keep actual data cards
        local_cards.append(card)

    # Store tokens in cache for each identified account
    tokens_received_count = 0
    for p_id, p_tokens, a_id, a_name in tokens_to_store:
        actual_acc_id = await token_cache.store(p_id, p_tokens, a_id, a_name)
        tokens_received_count += len(p_tokens)
        logger.info(f"Received {len(p_tokens)} tokens for {p_id} account {actual_acc_id} from {request.provider}")

    # Store local data metrics
    if local_cards:
        await external_metric_service.metrics_update_from_ingest(
            request.provider, local_cards
        )
        logger.info(f"Stored {len(local_cards)} metrics from {request.provider}")

    return {
        "status": "ok",
        "provider": request.provider,
        "tokens_received": tokens_received_count,
        "metrics_stored": len(local_cards),
    }
