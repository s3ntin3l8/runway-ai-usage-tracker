"""Explicit ordinary Chat observation with existing admin, Host and CSRF protection."""

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from app.api.endpoints.claude_cloud_probe import SanitizedProbeRoute
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.models.claude_chat_fixture import ChatFixtureReport, ChatFixtureRequest
from app.services.claude_chat_fixture import inspect_chat_fixture

router = APIRouter(route_class=SanitizedProbeRoute)


@router.post("/debug/claude-chat-fixture", response_model=ChatFixtureReport)
@limiter.limit("2/minute")
async def observe_chat_fixture(
    request: Request,
    body: ChatFixtureRequest,
    _auth: None = Depends(require_admin_key),
) -> Any:
    """Observe only an exact private target with one already-cached manual cookie."""
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        return await inspect_chat_fixture(body, client)
