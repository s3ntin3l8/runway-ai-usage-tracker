import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlmodel import Session

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import resolve_user_tz

# Window type rank for selecting the "longest" window among multiple cards.
# Higher rank = longer / more authoritative window.


def _validate_iso_param(name: str, value: str | None) -> None:
    """Validate an ISO-8601 query parameter, raising 422 on malformed input."""
    if value:
        try:
            parse_iso8601_utc(value)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{name} must be a valid ISO-8601 datetime")


def _local_period_anchors(tz: ZoneInfo, *, now: datetime | None = None) -> dict[str, Any]:
    """Current month/year boundaries in `tz`, as UTC instants + key strings.

    The "This period" / "Yearly" cumulative gauges reset on the user's local
    calendar, so they are anchored at local-midnight period starts converted
    back to UTC instants for the event range scan. `now` (a tz-aware instant)
    is injectable for deterministic tests; it defaults to the current time.
    """
    now_local = (now or datetime.now(UTC)).astimezone(tz)
    month_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_local = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "month_start_utc": month_local.astimezone(UTC),
        "year_start_utc": year_local.astimezone(UTC),
        "current_month": now_local.strftime("%Y-%m"),
        "current_year": now_local.strftime("%Y"),
    }


_MONTH_KEY_RE = re.compile(r"^\d{4}-\d{2}$")

# TTL response-cache config for the read-heavy dashboard endpoints (see
# app/core/cache.py). TTLs are set below each endpoint's own client poll
# cadence (webapp/src/features/home/queries.ts) so a full recompute happens
# at most once per cadence, not once per dashboard load/tab.
#
# Every cached response carries a `generated_at` field, frozen at the moment
# it was actually computed (cache miss) — a cache hit replays that same
# timestamp verbatim rather than stamping "now". This is deliberate: the
# frontend renders it as an "updated N ago" freshness indicator
# (HomePage.tsx), and it must report how old the underlying computation
# really is (up to the TTL above) — restamping it at serve time would make
# a cache hit falsely claim to be as fresh as a live recompute.
_FLEET_CACHE_KEY = "fleet"
_FLEET_CACHE_TTL = 30.0
_GLOBAL_STATS_CACHE_KEY = "global-stats"
_GLOBAL_STATS_CACHE_TTL = 300.0
_USAGE_SOURCES_CACHE_KEY = "usage-sources:v2"
_USAGE_SOURCES_CACHE_TTL = 60.0
_TOP_N_CACHE_TTL = 300.0
_FORECAST_CACHE_TTL = 45.0


def _normalize_filter(value: str | None) -> str | None:
    """Canonicalize an optional string filter used both as a cache key
    component and a query predicate, so equivalent requests that differ only
    in casing/whitespace share one cache entry and one SQL path instead of
    silently forking into two."""
    if value is None:
        return None
    return value.strip().lower() or None


def _month_bounds_utc(tz: ZoneInfo, period_key: str) -> tuple[datetime, datetime]:
    """[start, end) UTC instants for a 'YYYY-MM' month on the user's local calendar.

    Mirrors the frontend's startOfMonthISO/endOfMonthISO so historical month
    drill-downs share the same tz-correct boundaries the live current-month
    gauge uses — the UTC-keyed rollup can land on the wrong calendar month for
    users far from UTC.
    """
    year, month = (int(part) for part in period_key.split("-"))
    start_local = datetime(year, month, 1, tzinfo=tz)
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end_local = datetime(end_year, end_month, 1, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _resolve_ranking_since(session: Session, since: str | None, days: float | None) -> datetime:
    """Lower bound for the Top-* rankings: explicit `since` wins, else `days`
    back from now, else the start of the current month on the user's calendar
    (the UTC rollup lags that boundary, so these scan events live)."""
    if since:
        return parse_iso8601_utc(since)
    if days is not None:
        return datetime.now(UTC) - timedelta(days=days)
    return _local_period_anchors(resolve_user_tz(session))["month_start_utc"]


__all__ = [
    "_local_period_anchors",
    "_month_bounds_utc",
    "_normalize_filter",
    "_resolve_ranking_since",
    "_validate_iso_param",
]
