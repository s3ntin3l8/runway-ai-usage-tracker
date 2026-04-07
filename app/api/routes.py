from fastapi import APIRouter
from app.models.schemas import LimitsResponse
from app.services.collector_manager import CollectorManager
from app.api.endpoints.ingest import router as ingest_router
from app.api.endpoints.health import router as health_router

router = APIRouter()
router.include_router(ingest_router, tags=["ingest"])
router.include_router(health_router, tags=["health"])
manager = CollectorManager()

@router.get("/limits", response_model=LimitsResponse)
async def fetch_all_limits():
    """Fetch all AI service usage limits."""
    results = await manager.collect_all()
    return {"limits": results}
