from fastapi import APIRouter

from app.api.endpoints.github_oauth import router as github_router

router = APIRouter()

# Register auth providers
router.include_router(github_router, prefix="/github", tags=["auth", "github"])
