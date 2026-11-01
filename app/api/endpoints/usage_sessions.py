import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from app.api.endpoints._shared import _validate_iso_param

if TYPE_CHECKING:
    pass

from fastapi import APIRouter, Depends, Query, Request
from sqlmodel import Session

from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.services.event_query import (
    query_sessions,
)
from app.services.queries import (
    query_session_filter_options,
    query_sessions_paginated,
)

# Window type rank for selecting the "longest" window among multiple cards.
# Higher rank = longer / more authoritative window.
WINDOW_RANK: dict[str, int] = {"monthly": 4, "weekly": 3, "daily": 2, "session": 1}

router = APIRouter()


@router.get("/sessions")
@limiter.limit("30/minute")
async def get_usage_sessions(
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    sort_by: Literal["tokens", "recent"] = Query(default="tokens"),
    sidecar_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Top-N sessions within the requested time window.

    Defaults to the last 7 days when 'since' is not provided. An optional
    'until' (exclusive) upper bound scopes the window to a closed period (e.g.
    a selected past month). Events without a session_id are excluded.
    sort_by='tokens' (default) orders by token total desc; 'recent' orders by ts_end desc.
    """
    since_dt: datetime | None = None
    until_dt: datetime | None = None
    if since:
        since_dt = parse_iso8601_utc(since)
    if until:
        until_dt = parse_iso8601_utc(until)

    sessions = query_sessions(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since_dt,
        until=until_dt,
        limit=limit,
        sort_by=sort_by,
        sidecar_id=sidecar_id,
    )
    return {"sessions": sessions}


@router.get("/sessions/paginated")
@limiter.limit("30/minute")
async def get_usage_sessions_paginated(  # noqa: PLR0913, PLR0917 — known-debt: query filters + sort; collapse into a Pydantic params model in a follow-up
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    project: str | None = Query(default=None),
    page: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=50),
    sort_by: Literal["recent", "tokens", "duration", "messages", "cost"] = Query(default="recent"),
    sort_dir: Literal["asc", "desc"] = Query(default="desc"),
    sidecar_id: str | None = None,
    model: str | None = Query(default=None, min_length=1, max_length=200),
    branch: str | None = Query(default=None, min_length=1, max_length=250),
    subagents: Literal["exclude"] | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """One page of sessions (default 25) plus the total count, for the Sessions
    browser tab. Optional `project` filters to one working directory
    (`__none__` selects events without a project); `model` and `branch` narrow
    to events of that model id / git branch; `subagents=exclude` drops subagent
    events from the totals. All are event-level and intersect. Sorting is
    server-side over the full result set; `sort_by` picks the column (duration /
    messages / tokens / cost / recent) and `sort_dir` the direction."""
    _validate_iso_param("since", since)
    _validate_iso_param("until", until)
    # Synchronous SQLite work: off the event loop, so a cold aggregation does
    # not stall the server (and the sidecar ingests that arrive meanwhile).
    return await asyncio.to_thread(
        query_sessions_paginated,
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=parse_iso8601_utc(since) if since else None,
        until=parse_iso8601_utc(until) if until else None,
        page=page,
        page_size=limit,
        sort_by=sort_by,
        sort_dir=sort_dir,
        project=project,
        sidecar_id=sidecar_id,
        model=model,
        branch=branch,
        exclude_subagents=subagents == "exclude",
    )


async def _session_options(
    kind: str,
    *,
    provider_id: str,
    account_id: str,
    since: str | None,
    until: str | None,
    sidecar_id: str | None,
    project: str | None,
    q: str | None,
    limit: int,
    session: Session,
) -> list[str]:
    _validate_iso_param("since", since)
    _validate_iso_param("until", until)
    return await asyncio.to_thread(
        query_session_filter_options,
        session,
        kind=kind,
        provider_id=provider_id,
        account_id=account_id,
        since=parse_iso8601_utc(since) if since else None,
        until=parse_iso8601_utc(until) if until else None,
        sidecar_id=sidecar_id,
        project=project,
        search=q.strip() if q and q.strip() else None,
        limit=limit,
    )


@router.get("/session-models")
@limiter.limit("60/minute")
async def get_session_models(  # noqa: PLR0913 — mirrors the sessions window filters
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    sidecar_id: str | None = None,
    project: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Top models (by event count) in the sessions window, for the Model filter.
    Honors `project` so the list only offers models that exist under it."""
    return {
        "models": await _session_options(
            "model",
            provider_id=provider_id,
            account_id=account_id,
            since=since,
            until=until,
            sidecar_id=sidecar_id,
            project=project,
            q=q,
            limit=limit,
            session=session,
        )
    }


@router.get("/session-branches")
@limiter.limit("60/minute")
async def get_session_branches(  # noqa: PLR0913 — mirrors the sessions window filters
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    sidecar_id: str | None = None,
    project: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Top git branches (by event count) in the sessions window, for the Branch filter.
    Honors `project` so the list only offers branches that exist under it."""
    return {
        "branches": await _session_options(
            "branch",
            provider_id=provider_id,
            account_id=account_id,
            since=since,
            until=until,
            sidecar_id=sidecar_id,
            project=project,
            q=q,
            limit=limit,
            session=session,
        )
    }


__all__ = [
    "_session_options",
    "get_session_branches",
    "get_session_models",
    "get_usage_sessions",
    "get_usage_sessions_paginated",
]
