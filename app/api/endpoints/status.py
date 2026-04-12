from fastapi import APIRouter, Request
from app.services.collector_manager import manager
from app.core.rate_limit import limiter
from typing import Dict, Any

router = APIRouter()

@router.get("/")
@limiter.limit("30/minute")
async def get_collector_status(request: Request) -> Dict[str, Any]:
    """Return detailed health and cache stats for all active collectors."""
    try:
        await manager._sync_collectors()
    except Exception as e:
        logger.error(f"Failed to sync collectors for status: {e}")
    return manager.get_collector_stats()
