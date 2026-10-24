"""Explicit admin-only cloud probe; validation never echoes identifiers or tokens."""

import json
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.requests import ClientDisconnect
from starlette.types import Message

from app.core.rate_limit import limiter
from app.core.security import SESSION_COOKIE, require_admin_key
from app.models.claude_cloud_probe import ClaudeCloudProbeReport, ClaudeCloudProbeRequest
from app.services.claude_cloud_probe import probe_cloud_history


def _request_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_request_key")
        result[key] = value
    return result


class SanitizedProbeRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            await require_admin_key(
                request,
                request.headers.get("x-admin-key", ""),
                request.cookies.get(SESSION_COOKIE, ""),
            )
            chunks = []
            size = 0
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 8192:
                        return JSONResponse(
                            status_code=413, content={"detail": "probe_request_too_large"}
                        )
                    chunks.append(chunk)
            except ClientDisconnect:
                return JSONResponse(
                    status_code=400, content={"detail": "probe_request_disconnected"}
                )
            body = b"".join(chunks)
            try:
                json.loads(body.decode("utf-8"), object_pairs_hook=_request_object)
            except (ValueError, RecursionError):
                return JSONResponse(status_code=422, content={"detail": "invalid_probe_request"})
            sent = False

            async def receive() -> Message:
                nonlocal sent
                if sent:
                    return await request.receive()
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            bounded = Request(request.scope, receive)
            try:
                return await original(bounded)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"detail": "invalid_probe_request"})

        return handle


router = APIRouter(route_class=SanitizedProbeRoute)


@router.post("/debug/claude-cloud", response_model=ClaudeCloudProbeReport)
@limiter.limit("2/minute")
async def probe_claude_cloud(
    request: Request,
    body: ClaudeCloudProbeRequest,
    _auth: None = Depends(require_admin_key),
) -> Any:
    """Sample one exact cached source without refreshing, discovering or saving it."""
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        return await probe_cloud_history(body, client)
