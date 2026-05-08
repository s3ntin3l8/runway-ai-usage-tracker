"""Read-side query helpers for the event-sourced usage data model.

Provides four query functions consumed by the /api/v1/usage/{events,
window-history, heatmap, sessions} endpoints.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from app.models.db import UsageEvent, UsageWindow

# ---------------------------------------------------------------------------
# 7.1  query_events
# ---------------------------------------------------------------------------


def query_events(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    model_id: str | None = None,
    sidecar_id: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    order: str = "desc",
) -> list[UsageEvent]:
    """Return raw UsageEvent rows filtered and ordered by ts.

    Rows are returned newest-first (order='desc') or oldest-first
    (order='asc').  raw_json exclusion is handled by the endpoint layer.
    """
    stmt = select(UsageEvent).where(
        UsageEvent.provider_id == provider_id,
        UsageEvent.account_id == account_id,
    )

    if since is not None:
        stmt = stmt.where(UsageEvent.ts >= since)
    if until is not None:
        stmt = stmt.where(UsageEvent.ts <= until)
    if model_id is not None:
        stmt = stmt.where(UsageEvent.model_id == model_id)
    if sidecar_id is not None:
        stmt = stmt.where(UsageEvent.sidecar_id == sidecar_id)
    if kind is not None:
        stmt = stmt.where(UsageEvent.kind == kind)

    if order == "asc":
        stmt = stmt.order_by(UsageEvent.ts.asc())  # type: ignore[attr-defined]
    else:
        stmt = stmt.order_by(UsageEvent.ts.desc())  # type: ignore[attr-defined]

    stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


# ---------------------------------------------------------------------------
# 7.2  query_window_history
# ---------------------------------------------------------------------------


def query_window_history(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Return the N most recent closed windows, each with totals/by_model/by_sidecar.

    Row classification (per spec §6.2):
    - model_id='' AND sidecar_id=''  → totals + limit_value / pct_used
    - model_id!='' AND sidecar_id='' → by_model[]
    - model_id='' AND sidecar_id!='' → by_sidecar[]
    - both non-empty                 → dropped (cross-product rows)
    """
    stmt = (
        select(UsageWindow)
        .where(
            UsageWindow.provider_id == provider_id,
            UsageWindow.account_id == account_id,
            UsageWindow.window_type == window_type,
        )
        .order_by(UsageWindow.window_end.desc())  # type: ignore[attr-defined]
    )

    # Identify the N most-recent window_end values (the "top N windows")
    all_rows = list(session.exec(stmt).all())
    if not all_rows:
        return []

    # Collect unique window_ends in desc order, take the top N
    seen_ends: list[datetime] = []
    for row in all_rows:
        if row.window_end not in seen_ends:
            seen_ends.append(row.window_end)
        if len(seen_ends) == limit:
            break

    # Filter rows to only those in the selected window_ends
    allowed_ends = set(seen_ends)
    rows = [r for r in all_rows if r.window_end in allowed_ends]

    # Group by (window_start, window_end)
    window_map: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (r.window_start, r.window_end)
        if key not in window_map:
            window_map[key] = {
                "window_start": r.window_start.isoformat(),
                "window_end": r.window_end.isoformat(),
                "totals": None,
                "by_model": [],
                "by_sidecar": [],
                "limit_value": None,
                "pct_used": None,
            }

        if r.model_id == "" and r.sidecar_id == "":
            # all-up totals row
            window_map[key]["totals"] = _window_row_totals(r)
            window_map[key]["limit_value"] = r.limit_value
            window_map[key]["pct_used"] = r.pct_used

        elif r.model_id != "" and r.sidecar_id == "":
            # per-model row
            window_map[key]["by_model"].append({"model_id": r.model_id, **_window_row_totals(r)})

        elif r.model_id == "" and r.sidecar_id != "":
            # per-sidecar row
            window_map[key]["by_sidecar"].append(
                {"sidecar_id": r.sidecar_id, **_window_row_totals(r)}
            )
        # else: cross-product row (both non-empty) — drop per spec

    # Sort by window_end desc (most recent first)
    result = sorted(window_map.values(), key=lambda w: w["window_end"], reverse=True)

    # Fill empty totals with zeroed dict if no totals row was present
    for w in result:
        if w["totals"] is None:
            w["totals"] = {
                "msgs": 0,
                "tokens_input": 0,
                "tokens_output": 0,
                "tokens_cache_read": 0,
                "tokens_cache_create": 0,
                "tokens_reasoning": 0,
                "cost_usd": 0.0,
            }

    return result


def _window_row_totals(row: UsageWindow) -> dict[str, Any]:
    return {
        "msgs": row.msgs,
        "tokens_input": row.tokens_input,
        "tokens_output": row.tokens_output,
        "tokens_cache_read": row.tokens_cache_read,
        "tokens_cache_create": row.tokens_cache_create,
        "tokens_reasoning": row.tokens_reasoning,
        "cost_usd": row.cost_usd,
    }


# ---------------------------------------------------------------------------
# 7.3  query_heatmap
# ---------------------------------------------------------------------------


def query_heatmap(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    days: int = 14,
) -> list[dict[str, int]]:
    """Return a 7×24 grid of token totals (tokens_input + tokens_output +
    tokens_cache_read + tokens_cache_create) grouped by day-of-week and hour.

    Always returns all 168 cells; missing cells have tokens=0.
    dow follows SQLite strftime('%w'): 0=Sunday … 6=Saturday.
    """
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

    # Build lookup
    heat: dict[tuple[int, int], int] = {}
    for row in rows:
        heat[(int(row.dow), int(row.hour))] = int(row.tokens or 0)

    # Pad all 168 cells
    cells: list[dict[str, int]] = []
    for dow in range(7):
        for hour in range(24):
            cells.append({"dow": dow, "hour": hour, "tokens": heat.get((dow, hour), 0)})

    return cells


# ---------------------------------------------------------------------------
# 7.4  query_sessions
# ---------------------------------------------------------------------------


def query_sessions(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return top-N sessions by total tokens, newest first within the window.

    Each row includes ts_start, ts_end, duration_seconds, msgs, models[],
    tokens_total, cost_usd, sidecar_id.

    Events with NULL session_id are excluded.
    """
    if since is None:
        since = datetime.now(UTC) - timedelta(days=7)

    # Main aggregation query
    agg_sql = text(
        """
        SELECT
            session_id,
            MIN(ts)                                            AS ts_start,
            MAX(ts)                                            AS ts_end,
            COUNT(*)                                           AS msgs,
            SUM(tokens_input + tokens_output
                + tokens_cache_read + tokens_cache_create
                + tokens_reasoning)                            AS tokens_total,
            SUM(cost_usd)                                      AS cost_usd,
            MAX(sidecar_id)                                    AS sidecar_id,
            GROUP_CONCAT(DISTINCT model_id)                    AS models_csv
        FROM usage_events
        WHERE provider_id = :provider_id
          AND account_id  = :account_id
          AND session_id IS NOT NULL
          AND ts >= :since
        GROUP BY session_id
        ORDER BY tokens_total DESC
        LIMIT :limit
        """
    )

    rows = session.exec(  # type: ignore[call-overload]
        agg_sql,
        params={
            "provider_id": provider_id,
            "account_id": account_id,
            "since": since.isoformat(),
            "limit": limit,
        },
    ).all()

    results: list[dict[str, Any]] = []
    for row in rows:
        ts_start = _parse_ts(row.ts_start)
        ts_end = _parse_ts(row.ts_end)
        duration = int((ts_end - ts_start).total_seconds()) if ts_start and ts_end else 0

        # Parse model list from GROUP_CONCAT result
        models: list[str] = []
        if row.models_csv:
            models = [m for m in row.models_csv.split(",") if m]

        results.append(
            {
                "session_id": row.session_id,
                "ts_start": ts_start.isoformat() if ts_start else None,
                "ts_end": ts_end.isoformat() if ts_end else None,
                "duration_seconds": duration,
                "msgs": int(row.msgs),
                "models": models,
                "tokens_total": int(row.tokens_total or 0),
                "cost_usd": float(row.cost_usd or 0.0),
                "sidecar_id": row.sidecar_id,
            }
        )

    return results


def _parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse a timestamp from either a string or datetime object returned by SQLite."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    # SQLite returns ISO strings; may have 'T' or space separator, with/without tz
    s = str(value).replace(" ", "T")
    if not s.endswith("Z") and "+" not in s and len(s) <= 19:
        s += "+00:00"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
