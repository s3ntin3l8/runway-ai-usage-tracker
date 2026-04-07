from fastapi import APIRouter
from app.services.token_cache import token_cache
from app.services.external_metrics import external_metric_service
import time
from typing import Dict, Any

router = APIRouter()

@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """Check system health and collector status."""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "collectors": {
            "token_cache": {
                "providers": token_cache.get_all_stats(),
                "count": len(token_cache._cache)
            },
            "external_metrics": {
                "active_providers": list(external_metric_service.metrics.keys()),
                "count": len(external_metric_service.metrics)
            }
        }
    }
