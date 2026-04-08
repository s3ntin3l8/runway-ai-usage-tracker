from fastapi import APIRouter
from app.models.schemas import LimitsResponse, LimitCard
from app.services.collector_manager import CollectorManager
from app.api.endpoints.ingest import router as ingest_router
from app.api.endpoints.health import router as health_router

router = APIRouter()
router.include_router(ingest_router, tags=["ingest"])
router.include_router(health_router, tags=["health"])
manager = CollectorManager()

@router.get("/limits")
async def fetch_all_limits():
    """Fetch all AI service usage limits."""
    results = await manager.collect_all()
    
    # Validate and serialize with None values included
    limit_cards = [LimitCard(**item) for item in results]
    response = LimitsResponse(limits=limit_cards)
    
    # Return dict with None values included (needed for tier field)
    return response.model_dump(exclude_none=False)
