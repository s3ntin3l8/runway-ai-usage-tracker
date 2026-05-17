"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlmodel import Session, select

from app.models.db import UsageEvent

# ---------------------------------------------------------------------------
# 7.3  query_heatmap
# ---------------------------------------------------------------------------


def query_heatmap(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    days: int = 14,
    tz: str | None = None,
) -> list[dict[str, int]]:
    """Return a 7×24 grid of token totals (tokens_input + tokens_output +
    tokens_cache_read + tokens_cache_create) grouped by day-of-week and hour.

    Always returns all 168 cells; missing cells have tokens=0.
    dow follows SQLite strftime('%w'): 0=Sunday … 6=Saturday.

    When `tz` is a valid IANA name, events are converted to that zone before
    bucketing (so "14:00" in the heatmap corresponds to 14:00 in the user's
    local time). When `tz` is None or invalid, falls back to UTC bucketing
    via the original SQLite `strftime` aggregation.
    """
    zone: ZoneInfo | None = None
    if tz:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            zone = None

    if zone is None:
        return _heatmap_utc(session, provider_id=provider_id, account_id=account_id, days=days)
    return _heatmap_local(
        session, provider_id=provider_id, account_id=account_id, days=days, zone=zone
    )


def _heatmap_utc(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    days: int,
) -> list[dict[str, int]]:
    since_clause = f"-{days} days"

    sql = text(
        """
        SELECT
            CAST(strftime('%w', ts) AS INTEGER) AS dow,
            CAST(strftime('%H', ts) AS INTEGER) AS hour,
            SUM(tokens_input + tokens_output + tokens_cache_read + tokens_cache_create) AS tokens
        FROM usage_events
        WHERE provider_id = :provider_id
          AND account_id  = :account_id
          AND ts >= datetime('now', :since_clause)
        GROUP BY dow, hour
        """
    )

    rows = session.exec(
        sql,
        params={"provider_id": provider_id, "account_id": account_id, "since_clause": since_clause},
    ).all()  # type: ignore[call-overload]

    heat: dict[tuple[int, int], int] = {}
    for row in rows:
        heat[(int(row.dow), int(row.hour))] = int(row.tokens or 0)

    return _pad_cells(heat)


def _heatmap_local(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    days: int,
    zone: ZoneInfo,
) -> list[dict[str, int]]:
    since = datetime.now(UTC) - timedelta(days=days)

    rows = session.exec(  # type: ignore[call-overload]
        select(  # type: ignore[call-overload]
            UsageEvent.ts,
            UsageEvent.tokens_input,
            UsageEvent.tokens_output,
            UsageEvent.tokens_cache_read,
            UsageEvent.tokens_cache_create,
        ).where(
            UsageEvent.provider_id == provider_id,
            UsageEvent.account_id == account_id,
            UsageEvent.ts >= since,
        )
    ).all()

    heat: dict[tuple[int, int], int] = {}
    for ts, ti, to, tcr, tcc in rows:
        # SQLite stores naive UTC; coerce before tz conversion.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        local = ts.astimezone(zone)
        # Remap Python's Mon=0..Sun=6 → SQLite's Sun=0..Sat=6 wire convention.
        py_dow = local.weekday()  # Mon=0..Sun=6
        dow = (py_dow + 1) % 7  # Sun=0..Sat=6
        key = (dow, local.hour)
        heat[key] = heat.get(key, 0) + int(ti or 0) + int(to or 0) + int(tcr or 0) + int(tcc or 0)

    return _pad_cells(heat)


def _pad_cells(heat: dict[tuple[int, int], int]) -> list[dict[str, int]]:
    cells: list[dict[str, int]] = []
    for dow in range(7):
        for hour in range(24):
            cells.append({"dow": dow, "hour": hour, "tokens": heat.get((dow, hour), 0)})
    return cells
