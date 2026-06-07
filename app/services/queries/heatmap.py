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
    now: datetime | None = None,
) -> list[dict[str, float]]:
    """Return a 7×24 grid of token + cost totals grouped by day-of-week and hour.

    Tokens are the sum of tokens_input + tokens_output + tokens_cache_read +
    tokens_cache_create. Cost is the sum of cost_usd.

    Always returns all 168 cells; missing cells have tokens=0 and cost_usd=0.
    dow follows SQLite strftime('%w'): 0=Sunday … 6=Saturday.

    When `tz` is a valid IANA name, events are converted to that zone before
    bucketing (so "14:00" in the heatmap corresponds to 14:00 in the user's
    local time). When `tz` is None or invalid, falls back to UTC bucketing
    via the original SQLite `strftime` aggregation.

    `now` anchors the rolling `days` window (UTC); None means the current
    time. Production never passes it — it exists so tests with fixed event
    timestamps don't age out of the window.
    """
    zone: ZoneInfo | None = None
    if tz:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            zone = None

    if zone is None:
        return _heatmap_utc(
            session, provider_id=provider_id, account_id=account_id, days=days, now=now
        )
    return _heatmap_local(
        session, provider_id=provider_id, account_id=account_id, days=days, zone=zone, now=now
    )


def _heatmap_utc(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    days: int,
    now: datetime | None = None,
) -> list[dict[str, float]]:
    since_clause = f"-{days} days"
    # SQLite treats a bound 'now' string the same as the literal, so the
    # default is byte-identical to the original datetime('now', ...) query.
    anchor = now.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") if now else "now"

    sql = text(
        """
        SELECT
            CAST(strftime('%w', ts) AS INTEGER) AS dow,
            CAST(strftime('%H', ts) AS INTEGER) AS hour,
            SUM(tokens_input + tokens_output + tokens_cache_read + tokens_cache_create) AS tokens,
            SUM(cost_usd) AS cost_usd
        FROM usage_events
        WHERE provider_id = :provider_id
          AND account_id  = :account_id
          AND ts >= datetime(:anchor, :since_clause)
        GROUP BY dow, hour
        """
    )

    rows = session.exec(
        sql,
        params={
            "provider_id": provider_id,
            "account_id": account_id,
            "anchor": anchor,
            "since_clause": since_clause,
        },
    ).all()  # type: ignore[call-overload]

    tokens_heat: dict[tuple[int, int], int] = {}
    cost_heat: dict[tuple[int, int], float] = {}
    for row in rows:
        key = (int(row.dow), int(row.hour))
        tokens_heat[key] = int(row.tokens or 0)
        cost_heat[key] = float(row.cost_usd or 0.0)

    return _pad_cells(tokens_heat, cost_heat)


def _heatmap_local(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    days: int,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> list[dict[str, float]]:
    since = (now or datetime.now(UTC)) - timedelta(days=days)

    rows = session.exec(  # type: ignore[call-overload]
        select(  # type: ignore[call-overload]
            UsageEvent.ts,
            UsageEvent.tokens_input,
            UsageEvent.tokens_output,
            UsageEvent.tokens_cache_read,
            UsageEvent.tokens_cache_create,
            UsageEvent.cost_usd,
        ).where(
            UsageEvent.provider_id == provider_id,
            UsageEvent.account_id == account_id,
            UsageEvent.ts >= since,
        )
    ).all()

    tokens_heat: dict[tuple[int, int], int] = {}
    cost_heat: dict[tuple[int, int], float] = {}
    for ts, ti, to, tcr, tcc, cost in rows:
        # SQLite stores naive UTC; coerce before tz conversion.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        local = ts.astimezone(zone)
        # Remap Python's Mon=0..Sun=6 → SQLite's Sun=0..Sat=6 wire convention.
        py_dow = local.weekday()  # Mon=0..Sun=6
        dow = (py_dow + 1) % 7  # Sun=0..Sat=6
        key = (dow, local.hour)
        tokens_heat[key] = (
            tokens_heat.get(key, 0) + int(ti or 0) + int(to or 0) + int(tcr or 0) + int(tcc or 0)
        )
        cost_heat[key] = cost_heat.get(key, 0.0) + float(cost or 0.0)

    return _pad_cells(tokens_heat, cost_heat)


def _pad_cells(
    tokens_heat: dict[tuple[int, int], int],
    cost_heat: dict[tuple[int, int], float],
) -> list[dict[str, float]]:
    cells: list[dict[str, float]] = []
    for dow in range(7):
        for hour in range(24):
            cells.append(
                {
                    "dow": dow,
                    "hour": hour,
                    "tokens": tokens_heat.get((dow, hour), 0),
                    "cost_usd": cost_heat.get((dow, hour), 0.0),
                }
            )
    return cells
