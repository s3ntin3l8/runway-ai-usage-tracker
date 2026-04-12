from fastapi import APIRouter, HTTPException, Request
from app.core.config import settings
from app.core.encryption import encryption_service
from typing import Dict, Any
from app.core.rate_limit import limiter

router = APIRouter()

@router.get("/")
@limiter.limit("30/minute")
async def get_app_settings(request: Request) -> Dict[str, Any]:
    """Return the current non-sensitive configuration."""
    return {
        "project_name": settings.PROJECT_NAME,
        "run_mode": settings.RUN_MODE,
        "app_host": settings.APP_HOST,
        "app_port": settings.APP_PORT,
        "encryption_enabled": encryption_service.is_enabled,
        "local_collector_enabled": settings.LOCAL_COLLECTOR_ENABLED,
        "local_credential_scraping": settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED,
        "ingest_api_key_is_default": settings.INGEST_API_KEY_IS_INSECURE_DEFAULT,
    }
