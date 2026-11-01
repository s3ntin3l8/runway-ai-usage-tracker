from fastapi import APIRouter, Depends

from app.api.endpoints.auth import router as auth_router
from app.api.endpoints.claude_chat_fixture import router as claude_chat_fixture_router
from app.api.endpoints.claude_cloud_list_boundary import router as claude_cloud_list_boundary_router
from app.api.endpoints.claude_cloud_probe import router as claude_cloud_probe_router
from app.api.endpoints.claude_cloud_verification import router as claude_cloud_verification_router
from app.api.endpoints.data_health import router as data_health_router
from app.api.endpoints.fleet import router as fleet_router
from app.api.endpoints.fleet_sources import router as fleet_sources_router
from app.api.endpoints.system_account_merges import router as system_account_merges_router
from app.api.endpoints.system_app_config import router as system_app_config_router
from app.api.endpoints.system_credentials import router as system_credentials_router
from app.api.endpoints.system_health import router as system_health_router
from app.api.endpoints.system_provider_config import router as system_provider_config_router
from app.api.endpoints.system_webhooks import router as system_webhooks_router
from app.api.endpoints.usage import router as usage_router
from app.core.security import require_admin_key

router = APIRouter()

# API v1 Router
v1_router = APIRouter(prefix="/v1")

# Register domain-specific routers to v1
v1_router.include_router(
    usage_router, prefix="/usage", tags=["usage"], dependencies=[Depends(require_admin_key)]
)
v1_router.include_router(fleet_router, prefix="/fleet", tags=["fleet"])
v1_router.include_router(fleet_sources_router, prefix="/fleet", tags=["fleet"])
v1_router.include_router(auth_router, prefix="/auth", tags=["auth"])
for _sr in (
    system_health_router,
    system_credentials_router,
    system_webhooks_router,
    system_account_merges_router,
    system_provider_config_router,
    system_app_config_router,
):
    v1_router.include_router(_sr, prefix="/system", tags=["system"])
v1_router.include_router(claude_chat_fixture_router, prefix="/system", tags=["system"])
v1_router.include_router(claude_cloud_list_boundary_router, prefix="/system", tags=["system"])
v1_router.include_router(claude_cloud_probe_router, prefix="/system", tags=["system"])
v1_router.include_router(claude_cloud_verification_router, prefix="/system", tags=["system"])
v1_router.include_router(data_health_router, prefix="/system/data-health", tags=["data-health"])

# Include v1 into the main API router
router.include_router(v1_router)
