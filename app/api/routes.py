from fastapi import APIRouter, Request, HTTPException
from typing import Optional, Dict, Any
from app.models.schemas import LimitsResponse, LimitCard
from app.services.collector_manager import manager
from app.api.endpoints.ingest import router as ingest_router
from app.api.endpoints.health import router as health_router
from app.api.endpoints.github_oauth import router as github_router
from app.api.endpoints.history import router as history_router
from app.api.endpoints.settings import router as settings_router
from app.api.endpoints.status import router as status_router
from app.core.rate_limit import limiter

router = APIRouter()
router.include_router(ingest_router, tags=["ingest"])
router.include_router(health_router, tags=["health"])
router.include_router(github_router, prefix="/github/oauth", tags=["github_oauth"])
router.include_router(history_router, prefix="/history", tags=["history"])
router.include_router(settings_router, prefix="/settings", tags=["settings"])
router.include_router(status_router, prefix="/status", tags=["status"])


@router.get("/limits")
@limiter.limit("10/minute")
async def fetch_all_limits(request: Request) -> Dict[str, Any]:
    """Fetch all AI service usage limits."""
    results = await manager.collect_all()

    # Validate and serialize with None values included
    limit_cards = [LimitCard(**item) for item in results]
    response = LimitsResponse(limits=limit_cards)

    # Return dict with None values included (needed for tier field)
    return response.model_dump(exclude_none=False)


@router.post("/reset/{provider}")
@limiter.limit("10/minute")
async def reset_provider(request: Request, provider: str, account_id: Optional[str] = None) -> Dict[str, Any]:
    """Reset terminal failure state for a provider."""
    if provider not in manager.collector_registry:
        raise HTTPException(
            status_code=404, detail=f"Provider '{provider}' not found"
        )
    await manager.reset_collector(provider, account_id)
    return {"status": "reset", "provider": provider, "account_id": account_id}
