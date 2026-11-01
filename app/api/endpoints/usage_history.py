from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session

from app.api.endpoints._shared import _validate_iso_param
from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.services.event_query import (
    query_chart,
    query_history_deltas,
    query_snapshots,
    query_window_detail,
    query_windows,
)

# Higher rank = longer / more authoritative window.

router = APIRouter()


@router.get("/history/windows")
@limiter.limit("60/minute")
async def get_history_windows(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=30.0, ge=0.01, le=365.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    window_type: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Paginated list of quota windows (closed + open), newest first.

    An explicit ``since`` (ISO-8601 datetime) overrides ``days``; ``until``
    adds an exclusive upper bound. Per-type token breakdowns
    (input/output/reasoning/cache_read/cache_create) are included in each row.
    """
    _validate_iso_param("since", since)
    _validate_iso_param("until", until)
    return query_windows(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
        since=since,
        until=until,
        window_type=window_type,
        page=page,
        limit=limit,
    )


@router.get("/history/snapshots")
@limiter.limit("60/minute")
async def get_history_snapshots(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=7.0, ge=0.01, le=365.0),
    window_type: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Flat paginated snapshot rows with per-series delta, newest first.

    `provider_id` accepts a single value (`?provider_id=claude`) or a
    comma-separated list (`?provider_id=claude,chatgpt`). Multi-value form
    lets the server paginate the filtered set so pages aren't sparse.
    """
    provider_ids: list[str] | None = None
    if provider_id:
        provider_ids = [p.strip() for p in provider_id.split(",") if p.strip()] or None
    return query_snapshots(
        session,
        provider_ids=provider_ids,
        account_id=account_id,
        days=days,
        window_type=window_type,
        page=page,
        limit=limit,
    )


@router.get("/history/chart")
@limiter.limit("30/minute")
async def get_history_chart(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=30.0, ge=0.01, le=365.0),
    metric: str = Query(default="percent", pattern="^(percent|tokens|cost)$"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    group: str | None = Query(default=None, pattern="^(provider)$"),
    sidecar_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Chart data: percent → fill curves; tokens/cost → daily bars.

    Defaults to the last `days`; an explicit `since`/`until` (exclusive) pair
    scopes the chart to a closed period (e.g. a selected past month).
    group="provider" collapses token/cost bars to one segment per provider —
    the cross-provider overall view.
    """
    return query_chart(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
        metric=metric,
        since=parse_iso8601_utc(since) if since else None,
        until=parse_iso8601_utc(until) if until else None,
        group=group,
        sidecar_id=sidecar_id,
    )


@router.get("/history/window-detail")
@limiter.limit("30/minute")
async def get_history_window_detail(
    request: Request,
    provider_id: str,
    account_id: str,
    window_type: str,
    window_start: str,
    window_end: str,
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Fill-up series and by-model breakdown for one expanded window."""
    try:
        # `.replace(" ", "+")` undoes URL decoding that strips the `+` from
        # timezone offsets in query strings. Helper can't apply this safely.
        ws = parse_iso8601_utc(window_start.replace(" ", "+"))
        we = parse_iso8601_utc(window_end.replace(" ", "+"))
    except ValueError:
        raise HTTPException(status_code=422, detail="window_start/window_end must be ISO datetime")
    return query_window_detail(
        session,
        provider_id=provider_id,
        account_id=account_id,
        window_type=window_type,
        window_start=ws,
        window_end=we,
        days=days,
    )


@router.get("/history/deltas")
@limiter.limit("30/minute")
async def get_usage_history_deltas(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=1.0, ge=0.01, le=90.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    sidecar_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Compute actual consumption deltas from usage_events.

    Returns token_delta_total (cache-inclusive), token_cache_total (the cache
    split so callers can honor the exclude-cache toggle), cost_delta_total,
    cost_cache_total (the cost cache split), provider_token_deltas,
    critical_series_count, and series_sampled. Per-type token breakdowns
    (input/output/reasoning/cache_read/cache_create) are also returned.

    An explicit ``since`` (ISO-8601 datetime) overrides ``days``; ``until``
    adds an exclusive upper bound.
    """
    _validate_iso_param("since", since)
    _validate_iso_param("until", until)
    return query_history_deltas(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
        since=since,
        until=until,
        sidecar_id=sidecar_id,
    )


__all__ = [
    "get_history_chart",
    "get_history_snapshots",
    "get_history_window_detail",
    "get_history_windows",
    "get_usage_history_deltas",
]
