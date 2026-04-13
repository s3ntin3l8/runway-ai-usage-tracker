from fastapi import APIRouter

from app.api.endpoints.auth import router as auth_router
from app.api.endpoints.fleet import router as fleet_router
from app.api.endpoints.system import router as system_router
from app.api.endpoints.usage import router as usage_router

router = APIRouter()

# API v1 Router
v1_router = APIRouter(prefix="/v1")

# Register domain-specific routers to v1
v1_router.include_router(usage_router, prefix="/usage", tags=["usage"])
v1_router.include_router(fleet_router, prefix="/fleet", tags=["fleet"])
v1_router.include_router(auth_router, prefix="/auth", tags=["auth"])
v1_router.include_router(system_router, prefix="/system", tags=["system"])

# Include v1 into the main API router
router.include_router(v1_router)
