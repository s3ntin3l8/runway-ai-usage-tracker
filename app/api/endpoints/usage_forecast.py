import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlmodel import Session, select

from app.api.endpoints._shared import _FORECAST_CACHE_TTL, _normalize_filter
from app.core.cache import cache_get, cache_set
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.models.schemas import (
    ForecastResponse,
    LimitCard,
)
from app.services.collector_manager import manager
from app.services.event_query import (
    query_cost_forecast,
)
from app.services.forecast import compute_all_forecasts

# Higher rank = longer / more authoritative window.

router = APIRouter()


@router.get("/forecast")
@limiter.limit("30/minute")
async def get_usage_forecast(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    window_type: str | None = None,
    include_series: bool = False,
    session: Session = Depends(get_session),
) -> ForecastResponse:
    """Project quota usage to reset time using linear extrapolation in the current window.

    `include_series=true` populates each entry's `series` field with the
    cumulative-pct bucket trajectory for client-side drill-down rendering.

    The record fetch and the forecast computation are each synchronous SQLite
    work, offloaded to a worker thread via `asyncio.to_thread` (see
    `fetch_fleet_view` for why); the rare bootstrap fallback below is real
    async I/O and stays on the event loop. Response is cached (keyed by every
    query param) for `_FORECAST_CACHE_TTL` — quota_snapshots only gains new
    rows on a poll cycle, so the forecast can't meaningfully change faster
    than that.
    """
    provider_id = _normalize_filter(provider_id)
    account_id = _normalize_filter(account_id)
    window_type = _normalize_filter(window_type)
    cache_key = f"forecast:{provider_id}:{account_id}:{window_type}:{include_series}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    from app.models.db import LatestUsage

    def _fetch_records() -> list[dict[str, Any]]:
        # Push known filters into SQL so we don't materialise the whole table
        # when the caller only wants one card's forecast.
        stmt = select(LatestUsage)
        if provider_id:
            stmt = stmt.where(LatestUsage.provider_id == provider_id)
        if account_id:
            stmt = stmt.where(LatestUsage.account_id == account_id)
        if window_type:
            stmt = stmt.where(LatestUsage.window_type == window_type)
        records = session.exec(stmt).all()
        parsed = []
        for r in records:
            try:
                parsed.append(json.loads(r.card_json))
            except (json.JSONDecodeError, TypeError):
                continue
        return parsed

    results = await asyncio.to_thread(_fetch_records)

    if not results:
        # Bootstrap fallback — only when the caller asked for everything.
        if not (provider_id or account_id or window_type):
            results = await manager.collect_all()

    result = await asyncio.to_thread(_compute_forecast_response, results, session, include_series)
    cache_set(cache_key, result, _FORECAST_CACHE_TTL)
    return result


def _compute_forecast_response(
    results: list[dict[str, Any]], session: Session, include_series: bool
) -> ForecastResponse:
    from app.services.forecast import compute_forecast

    cards = [LimitCard(**item) for item in results]

    if include_series:
        # Drill-down path: skip batch optimization, compute per-card with series.
        now = datetime.now(UTC)
        forecasts = []
        summary: dict[str, int] = {}
        for card in cards:
            entry = compute_forecast(card, session, now=now, include_series=True)
            if entry is not None:
                forecasts.append(entry)
                summary[entry.status] = summary.get(entry.status, 0) + 1
        return ForecastResponse(forecasts=forecasts, summary=summary, generated_at=now.isoformat())

    return compute_all_forecasts(cards, session)


@router.get("/cost-forecast")
@limiter.limit("30/minute")
async def get_cost_forecast(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    exclude_cache: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Month-to-date cost + 7-day burn extrapolation to end of month.

    Combines current MTD from period_type=month rollups with the average
    daily cost over the last 7 days to project end-of-month spend.
    Optionally filtered by provider_id and/or account_id.
    """
    return query_cost_forecast(
        session,
        provider_id=provider_id,
        account_id=account_id,
        exclude_cache=exclude_cache,
    )


__all__ = ["_compute_forecast_response", "get_cost_forecast", "get_usage_forecast"]
