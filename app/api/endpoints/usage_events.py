from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlmodel import Session

from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.models.utc_datetime import iso_utc
from app.services.event_query import (
    query_events,
    query_heatmap,
    query_window_history,
)
from app.services.queries import (
    count_events,
    event_time_range,
)

# Higher rank = longer / more authoritative window.

router = APIRouter()


@router.get("/events")
@limiter.limit("30/minute")
async def get_usage_events(  # noqa: PLR0913, PLR0917 — known-debt: 12 query filters; collapse into a Pydantic params model in a follow-up
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
    sidecar_id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="desc"),
    include_raw: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Recent event tail for a (provider_id, account_id) pair.

    Supports filtering by time range, model, sidecar, and kind, plus
    offset/limit pagination. Returns events newest-first by default. ``total``
    is the full count of matching rows (not the page size), so callers can page.
    raw_json is excluded unless include_raw=true. Use kind=error to retrieve
    only provider failure events.
    """
    since_dt: datetime | None = None
    until_dt: datetime | None = None
    if since:
        since_dt = parse_iso8601_utc(since)
    if until:
        until_dt = parse_iso8601_utc(until)

    events = query_events(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since_dt,
        until=until_dt,
        model_id=model_id,
        sidecar_id=sidecar_id,
        kind=kind,
        limit=limit,
        offset=offset,
        order=order,
    )

    total = count_events(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since_dt,
        until=until_dt,
        model_id=model_id,
        sidecar_id=sidecar_id,
        kind=kind,
    )

    rows = []
    for ev in events:
        d = ev.model_dump()
        if not include_raw:
            d.pop("raw_json", None)
        rows.append(d)

    return {"events": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/events/range")
@limiter.limit("30/minute")
async def get_usage_events_range(
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    sidecar_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, str | None]:
    """Earliest/latest event timestamps for a (provider_id, account_id) pair.

    Bounds the month selector — there's no point paging back before the first
    recorded event. Both fields are null when the pair has no events yet.
    """
    earliest, latest = event_time_range(
        session, provider_id=provider_id, account_id=account_id, sidecar_id=sidecar_id
    )
    return {"earliest": iso_utc(earliest), "latest": iso_utc(latest)}


@router.get("/window-history")
@limiter.limit("30/minute")
async def get_window_history(
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    window_type: str = Query(...),
    series_model_id: str | None = Query(default=None),
    series_variant: str | None = Query(default=None),
    limit: int = Query(default=12, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Closed-window history with per-model and per-sidecar splits.

    Returns up to N most recent closed windows for the given
    (provider_id, account_id, window_type) triple, ordered newest-first.
    Optional series identity scopes the result to one quota model/pool and
    includes legacy rows whose series identity was not recorded. An omitted or
    empty series_variant on a scoped request is normalized to "default", the
    variant used by newly closed windows for cards without an explicit variant.
    """
    windows = query_window_history(
        session,
        provider_id=provider_id,
        account_id=account_id,
        window_type=window_type,
        limit=limit,
        series_model_id=series_model_id,
        series_variant=series_variant,
    )
    return {"windows": windows}


@router.get("/heatmap")
@limiter.limit("30/minute")
async def get_usage_heatmap(
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    days: int = Query(default=14, ge=1, le=90),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    tz: str | None = Query(default=None, description="IANA timezone (e.g. Europe/Berlin)"),
    sidecar_id: str | None = None,
    exclude_cache: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """7×24 hour-of-day token activity grid.

    Defaults to the last N `days`; an explicit `since`/`until` (exclusive) pair
    scopes the grid to a closed period (e.g. a selected past month) instead.
    Always returns all 168 cells (7 days × 24 hours). Cells with no events
    have tokens=0. dow follows SQLite convention: 0=Sunday … 6=Saturday.

    When `tz` is a valid IANA name, events are bucketed in that zone (so the
    grid reflects local hour-of-day). Invalid or missing `tz` falls back to
    UTC bucketing.
    """
    since_dt = parse_iso8601_utc(since) if since else None
    until_dt = parse_iso8601_utc(until) if until else None
    cells = query_heatmap(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
        since=since_dt,
        until=until_dt,
        tz=tz,
        sidecar_id=sidecar_id,
        exclude_cache=exclude_cache,
    )
    return {"cells": cells, "tz": tz or "UTC"}


__all__ = ["get_usage_events", "get_usage_events_range", "get_usage_heatmap", "get_window_history"]
