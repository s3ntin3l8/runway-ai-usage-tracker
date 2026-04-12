from fastapi import APIRouter, Request
from app.models.schemas import LimitsResponse, LimitCard
from app.services.collector_manager import manager
from app.api.endpoints.ingest import router as ingest_router
from app.api.endpoints.health import router as health_router
from app.api.endpoints.github_oauth import router as github_router
from app.api.endpoints.history import router as history_router
from app.api.endpoints.settings import router as settings_router
from app.core.rate_limit import limiter

router = APIRouter()
router.include_router(ingest_router, tags=["ingest"])
router.include_router(health_router, tags=["health"])
router.include_router(github_router, prefix="/github/oauth", tags=["github_oauth"])
router.include_router(history_router, prefix="/history", tags=["history"])
router.include_router(settings_router, prefix="/settings", tags=["settings"])


@router.get("/limits")
@limiter.limit("10/minute")
async def fetch_all_limits(request: Request):
    """Fetch all AI service usage limits."""
    results = await manager.collect_all()

    # Validate and serialize with None values included
    limit_cards = [LimitCard(**item) for item in results]
    response = LimitsResponse(limits=limit_cards)

    # Return dict with None values included (needed for tier field)
    return response.model_dump(exclude_none=False)
