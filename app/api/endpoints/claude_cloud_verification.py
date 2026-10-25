"""Explicit operator actions; reads never advance a provider checkpoint."""

import asyncio
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from app.api.endpoints.claude_cloud_probe import SanitizedProbeRoute
from app.core.config import settings
from app.core.encryption import encryption_service
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.models.claude_cloud_verification import (
    VerificationCancel,
    VerificationEmpty,
    VerificationReport,
    VerificationStart,
    VerificationStatus,
    VerificationStep,
)
from app.services.claude_cloud_checkpoints import CheckpointError, CheckpointStore
from app.services.claude_cloud_probe import ProbeError
from app.services.claude_cloud_verification import (
    advance_verification,
    build_verification_report,
    read_verification,
    start_verification,
)


class VerificationRoute(SanitizedProbeRoute):
    max_request_bytes = 64 * 1024


def checkpoint_store() -> CheckpointStore:
    return CheckpointStore(Path(settings.DATABASE_PATH), encryption_service)


Action = Annotated[
    VerificationStart | VerificationStep | VerificationStatus | VerificationCancel,
    Field(discriminator="action"),
]
router = APIRouter(route_class=VerificationRoute)


@router.post(
    "/debug/claude-cloud-verification", response_model=VerificationReport | VerificationEmpty
)
@limiter.limit("6/minute")
async def verification_action(
    request: Request,
    body: Action,
    _auth: None = Depends(require_admin_key),
) -> Any:
    """Only step issues bounded GETs. Status and start use local state only."""
    try:
        store = checkpoint_store()
        if isinstance(body, VerificationCancel):
            await asyncio.to_thread(store.cancel, body.job_handle)
            return {"state": "cancelled"}
        if isinstance(body, VerificationStart):
            handle = await start_verification(body, store)
        elif isinstance(body, VerificationStatus) and body.job_handle is None:
            current = await asyncio.to_thread(store.current_handle)
            if current is None:
                return {"state": "absent"}
            handle = current
        else:
            assert body.job_handle is not None
            handle = body.job_handle
        if isinstance(body, VerificationStep):
            async with httpx.AsyncClient(
                timeout=5, follow_redirects=False, trust_env=False
            ) as client:
                state = await advance_verification(handle, store, client)
        else:
            state = await read_verification(handle, store)
        try:
            return VerificationReport.model_validate(build_verification_report(handle, state))
        except ValueError:
            # Framework response errors can log input values. Fail with a fixed category.
            raise HTTPException(503, detail="invalid_verification_report") from None
    except CheckpointError as exc:
        category = str(exc)
        if category in {"busy", "job_exists"}:
            raise HTTPException(409, detail=category) from None
        if category == "not_found":
            raise HTTPException(404, detail=category) from None
        detail = (
            "encryption_required" if category == "encryption_required" else "checkpoint_unavailable"
        )
        raise HTTPException(503, detail=detail) from None
    except ProbeError as exc:
        category = str(exc)
        if category not in {"source_unavailable", "source_ineligible", "unsupported_source"}:
            category = "source_unavailable"
        raise HTTPException(409, detail=category) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail="source_unavailable") from None
