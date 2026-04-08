from fastapi import APIRouter, HTTPException, Header, Request
from datetime import datetime, timezone
from typing import Dict, Optional, List
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
    x_timestamp: str = Header(None, alias="X-Timestamp")
):
    """
    Ingest metrics from sidecar with HMAC-SHA256 signature verification.

    Headers required:
    - X-Signature: HMAC-SHA256(secret, timestamp + body)
    - X-Timestamp: Unix timestamp (within 5 minutes)
    """
    # 0. Guard against misconfigured or insecure API key
    if not settings.INGEST_API_KEY:
        logger.error("INGEST_API_KEY is empty — ingest endpoint is disabled")
        raise HTTPException(status_code=503, detail="Ingest endpoint not configured: INGEST_API_KEY is empty")
    if settings.INGEST_API_KEY_IS_INSECURE_DEFAULT:
        logger.error("INGEST_API_KEY is the default insecure value — ingest endpoint is disabled")
        raise HTTPException(status_code=503, detail="Ingest endpoint not configured: INGEST_API_KEY must be changed from default")

    # 1. Check headers
    if not x_signature or not x_timestamp:
        logger.warning("Ingest attempt with missing HMAC headers")
        raise HTTPException(status_code=401, detail="Missing HMAC signature or timestamp")
    
    # 2. Check timestamp (5-minute window)
    try:
        ts = float(x_timestamp)
        now = time.time()
        if abs(now - ts) > 300:
            logger.warning(f"Ingest attempt with expired timestamp: {abs(now - ts):.0f}s difference")
            raise HTTPException(status_code=401, detail="Request timestamp expired")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid X-Timestamp format")
    
    # 3. Read body and verify signature
    body_bytes = await raw_request.body()
    expected_sig = hmac.new(
        settings.INGEST_API_KEY.encode(),
        f"{x_timestamp}".encode() + body_bytes,
        hashlib.sha256
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
    
    tokens = {}
    local_cards = []
    
    # Extract base provider name (e.g., "anthropic-laptop" -> "anthropic")
    provider_base = request.provider.split("-")[0]
    
    for card in request.metrics:
        detail = card.detail
        
        # Extract OAuth token and refresh token BEFORE modifying detail
        oauth_token = _extract_token(detail, "oauth_token:") if "oauth_token:" in detail else None
        refresh_token = _extract_token(detail, "refresh_token:") if "refresh_token:" in detail else None
        
        # Store tokens
        if oauth_token:
            tokens["oauth_token"] = oauth_token
            logger.debug(f"Extracted OAuth token for {provider_base}")
        
        if refresh_token:
            tokens["refresh_token"] = refresh_token
            logger.debug(f"Extracted refresh token for {provider_base}")
        
        # Redact tokens from detail string AFTER both are extracted
        if oauth_token:
            detail = detail.replace(f"oauth_token:{oauth_token}", "oauth_token:[REDACTED]")
        if refresh_token:
            detail = detail.replace(f"refresh_token:{refresh_token}", "refresh_token:[REDACTED]")
        if oauth_token or refresh_token:
            card.detail = detail
        
        # Check if this is a token-only card (should NOT be displayed)
        # Token-only cards have indicators like:
        # - remaining="Token" and unit="oauth"/"api_key"
        # - data_source="token_extracted"
        # - detail contains only redacted tokens
        is_token_only = (
            card.remaining == "Token" and card.unit in ("oauth", "api_key") and
            card.data_source == "token_extracted"
        )
        
        if is_token_only:
            # Skip token-only cards - they're just for token extraction, not display
            logger.debug(f"Skipping token-only card for {card.service}")
            continue
        
        # Extract cookie
        if "cookie:" in detail and not is_token_only:
            cookie_info = _extract_cookie(detail)
            if cookie_info:
                name, value = cookie_info
                tokens[f"cookie_{name}"] = value
                card.detail = detail.replace(f"cookie:{name}:{value}", f"cookie:{name}:[REDACTED]")
                local_cards.append(card)
                logger.debug(f"Extracted cookie '{name}' for {provider_base}")
        
        # Extract API key
        elif "api_key:" in detail and not is_token_only:
            key = _extract_token(detail, "api_key:")
            if key:
                tokens["api_key"] = key
                card.detail = detail.replace(f"api_key:{key}", "api_key:[REDACTED]")
                local_cards.append(card)
                logger.debug(f"Extracted API key for {provider_base}")
        
        # Keep actual data cards (local file readings)
        else:
            local_cards.append(card)
    
    # Store tokens in cache
    if tokens:
        token_cache.store(provider_base, tokens)
        logger.info(f"Received {len(tokens)} tokens from {request.provider}")
    
    # Store local data metrics
    if local_cards:
        external_metric_service.metrics[request.provider] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cards": [card.model_dump() for card in local_cards]
        }
        external_metric_service._save()
        logger.info(f"Stored {len(local_cards)} metrics from {request.provider}")
    
    return {
        "status": "ok",
        "provider": request.provider,
        "tokens_received": len(tokens),
        "metrics_stored": len(local_cards)
    }


def _extract_token(detail: str, prefix: str) -> Optional[str]:
    """Extract token value after prefix."""
    try:
        start = detail.index(prefix) + len(prefix)
        end = detail.find(" ", start)
        if end == -1:
            end = detail.find("·", start)
        if end == -1:
            end = detail.find("[", start)
        if end == -1:
            end = len(detail)
        return detail[start:end].strip()
    except ValueError:
        return None


def _extract_cookie(detail: str) -> Optional[tuple]:
    """Extract cookie name and value."""
    try:
        start = detail.index("cookie:") + len("cookie:")
        end = detail.find(" ", start)
        if end == -1:
            end = detail.find("·", start)
        if end == -1:
            end = detail.find("[", start)
        if end == -1:
            end = len(detail)
        
        cookie_str = detail[start:end].strip()
        parts = cookie_str.split(":", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
    except ValueError:
        pass
    return None
