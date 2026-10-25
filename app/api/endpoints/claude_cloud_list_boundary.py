"""Explicit admin diagnostic; never dispatched by status or page loads."""

from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.endpoints.claude_cloud_probe import SanitizedProbeRoute
from app.core.config import settings
from app.core.encryption import encryption_service
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.models.claude_cloud_list_boundary import ListBoundaryReport, ListBoundaryRequest
from app.services.claude_cloud_checkpoints import CheckpointStore
from app.services.claude_cloud_list_boundary import inspect_list_boundary

router = APIRouter(route_class=SanitizedProbeRoute)


@router.post("/debug/claude-cloud-list-boundary", response_model=ListBoundaryReport)
@limiter.limit("2/minute")
async def list_boundary(
    request: Request,
    body: ListBoundaryRequest,
    _auth: None = Depends(require_admin_key),
) -> Any:
    store = CheckpointStore(Path(settings.DATABASE_PATH), encryption_service)
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        report = await inspect_list_boundary(body.job_handle, store, client)
    try:
        return ListBoundaryReport.model_validate(report)
    except ValueError:
        raise HTTPException(503, detail="invalid_boundary_report") from None
