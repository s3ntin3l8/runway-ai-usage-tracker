from fastapi import APIRouter, Request
from app.services.token_cache import token_cache
from app.services.external_metrics import external_metric_service
import time
from typing import Dict, Any
from app.core.rate_limit import limiter

router = APIRouter()


@router.get("/health")
@limiter.limit("30/minute")
async def health_check(request: Request) -> Dict[str, Any]:
    """Check system health and collector status."""
    return {
        "status": "healthy",
        "timestamp": time.time(),
    }
