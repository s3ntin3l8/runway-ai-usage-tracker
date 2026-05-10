"""Read-side query helpers for the event-sourced usage data model.

Provides four query functions consumed by the /api/v1/usage/{events,
window-history, heatmap, sessions} endpoints, plus cost forecast and
anomaly detection helpers for the §12 optional enhancements.
"""

import calendar
import json
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, text
from sqlmodel import Session, select

from app.models._datetime import iso_utc
from app.models.db import UsageEvent, UsagePeriodRollup, UsageWindow
from app.services.window_closer import WINDOW_DURATION

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
                "window_start": iso_utc(r.window_start),
                "window_end": iso_utc(r.window_end),
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
                "ts_start": iso_utc(ts_start),
                "ts_end": iso_utc(ts_end),
                "duration_seconds": duration,
                "msgs": int(row.msgs),
                "models": models,
                "tokens_total": int(row.tokens_total or 0),
                "cost_usd": float(row.cost_usd or 0.0),
                "sidecar_id": row.sidecar_id,
            }
        )

    return results


# ---------------------------------------------------------------------------
# 15.1  query_window_aggregation
# ---------------------------------------------------------------------------


def query_window_aggregation(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str,
    reset_at: datetime,
) -> dict:
    """Aggregate usage_events for [reset_at - WINDOW_DURATION[window_type], reset_at)
    into token_usage + by_model + by_sidecar dicts."""
    duration = WINDOW_DURATION[window_type]
    window_start = reset_at - duration

    # One pass: per (model_id, sidecar_id) sums
    rows = session.exec(
        select(  # type: ignore[call-overload]
            UsageEvent.model_id,
            UsageEvent.sidecar_id,
            func.count(UsageEvent.id),  # type: ignore[arg-type]
            func.sum(UsageEvent.tokens_input),
            func.sum(UsageEvent.tokens_output),
            func.sum(UsageEvent.tokens_cache_read),
            func.sum(UsageEvent.tokens_cache_create),
            func.sum(UsageEvent.tokens_reasoning),
            func.sum(UsageEvent.cost_usd),
        )
        .where(
            UsageEvent.provider_id == provider_id,
            UsageEvent.account_id == account_id,
            UsageEvent.kind == "message",
            UsageEvent.ts >= window_start,
            UsageEvent.ts < reset_at,
        )
        .group_by(UsageEvent.model_id, UsageEvent.sidecar_id)
    ).all()

    # Roll up: total + by_model + by_sidecar
    total: dict[str, Any] = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_create": 0,
        "reasoning": 0,
        "msgs": 0,
        "cost": 0.0,
    }
    by_model: dict[str, dict] = {}
    by_sidecar: dict[str, dict] = {}

    for mid, sid, msgs, ti, to, tcr, tcc, tr, cost in rows:
        ti = ti or 0
        to = to or 0
        tcr = tcr or 0
        tcc = tcc or 0
        tr = tr or 0
        cost = cost or 0.0
        total["input"] += ti
        total["output"] += to
        total["cache_read"] += tcr
        total["cache_create"] += tcc
        total["reasoning"] += tr
        total["msgs"] += msgs
        total["cost"] += cost
        if mid:
            m = by_model.setdefault(
                mid,
                {
                    "tokens_input": 0,
                    "tokens_output": 0,
                    "tokens_cache_read": 0,
                    "tokens_cache_create": 0,
                    "tokens_reasoning": 0,
                    "msgs": 0,
                    "cost_usd": 0.0,
                },
            )
            m["tokens_input"] += ti
            m["tokens_output"] += to
            m["tokens_cache_read"] += tcr
            m["tokens_cache_create"] += tcc
            m["tokens_reasoning"] += tr
            m["msgs"] += msgs
            m["cost_usd"] += cost
        if sid:
            s = by_sidecar.setdefault(
                sid,
                {
                    "tokens_input": 0,
                    "tokens_output": 0,
                    "tokens_cache_read": 0,
                    "tokens_cache_create": 0,
                    "tokens_reasoning": 0,
                    "msgs": 0,
                    "cost_usd": 0.0,
                },
            )
            s["tokens_input"] += ti
            s["tokens_output"] += to
            s["tokens_cache_read"] += tcr
            s["tokens_cache_create"] += tcc
            s["tokens_reasoning"] += tr
            s["msgs"] += msgs
            s["cost_usd"] += cost

    token_usage = {
        "input": total["input"],
        "output": total["output"],
        "cache_read": total["cache_read"],
        "cache_create": total["cache_create"],
        "reasoning": total["reasoning"],
        "total": (
            total["input"]
            + total["output"]
            + total["cache_read"]
            + total["cache_create"]
            + total["reasoning"]
        ),
    }
    return {
        "window_type": window_type,
        "window_start": window_start.isoformat(),
        "window_end": reset_at.isoformat(),
        "token_usage": token_usage,
        "by_model": by_model,
        "by_sidecar": by_sidecar,
    }


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


# ---------------------------------------------------------------------------
# 14.2  query_cost_forecast
# ---------------------------------------------------------------------------


def query_cost_forecast(
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Return a cost forecast combining current MTD with 7-day burn average.

    Algorithm:
    - MTD: sum cost_usd from period_type=month, model_id='', sidecar_id='' for current month.
    - 7d avg: sum cost_usd from period_type=day, model_id='', sidecar_id='' for past 7 days
              divided by 7 (always divides by 7, zero-filling missing days).
    - projected_eom = MTD + (daily_avg × days_remaining).
    """
    now = datetime.now(UTC)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    day_of_month = now.day
    days_remaining = days_in_month - day_of_month
    month_key = now.strftime("%Y-%m")
    seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    # Fetch current-month rollup rows (all-up grain: model_id='', sidecar_id='')
    mtd_stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == "month",
        UsagePeriodRollup.period_key == month_key,
        UsagePeriodRollup.model_id == "",
        UsagePeriodRollup.sidecar_id == "",
    )
    if provider_id:
        mtd_stmt = mtd_stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        mtd_stmt = mtd_stmt.where(UsagePeriodRollup.account_id == account_id)
    mtd_rows = list(session.exec(mtd_stmt).all())

    # Fetch last-7-days daily rollup rows (all-up grain)
    daily_stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == "day",
        UsagePeriodRollup.model_id == "",
        UsagePeriodRollup.sidecar_id == "",
        UsagePeriodRollup.period_key >= seven_days_ago,
    )
    if provider_id:
        daily_stmt = daily_stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        daily_stmt = daily_stmt.where(UsagePeriodRollup.account_id == account_id)
    daily_rows = list(session.exec(daily_stmt).all())

    # Group by (provider_id, account_id)
    AccountKey = tuple[str, str]
    mtd_by_account: dict[AccountKey, float] = {}
    for r in mtd_rows:
        key: AccountKey = (r.provider_id, r.account_id)
        mtd_by_account[key] = mtd_by_account.get(key, 0.0) + r.cost_usd

    daily_sum_by_account: dict[AccountKey, float] = {}
    for r in daily_rows:
        key = (r.provider_id, r.account_id)
        daily_sum_by_account[key] = daily_sum_by_account.get(key, 0.0) + r.cost_usd

    # Build per-account breakdown
    all_keys: set[AccountKey] = set(mtd_by_account.keys()) | set(daily_sum_by_account.keys())
    by_provider: list[dict[str, Any]] = []
    total_mtd = 0.0
    total_7d_sum = 0.0

    for key in sorted(all_keys):
        pid, aid = key
        mtd = mtd_by_account.get(key, 0.0)
        seven_d_sum = daily_sum_by_account.get(key, 0.0)
        daily_avg = seven_d_sum / 7.0
        projected = mtd + daily_avg * days_remaining if daily_avg > 0 else mtd
        by_provider.append(
            {
                "provider_id": pid,
                "account_id": aid,
                "current_month_to_date": round(mtd, 6),
                "daily_burn_avg_7d": round(daily_avg, 6),
                "projected_eom": round(projected, 6),
            }
        )
        total_mtd += mtd
        total_7d_sum += seven_d_sum

    total_daily_avg = total_7d_sum / 7.0
    total_projected = (
        total_mtd + total_daily_avg * days_remaining if total_daily_avg > 0 else total_mtd
    )

    return {
        "as_of": now.isoformat(),
        "current_month_to_date": round(total_mtd, 6),
        "daily_burn_avg_7d": round(total_daily_avg, 6),
        "projected_eom": round(total_projected, 6),
        "days_in_month": days_in_month,
        "day_of_month": day_of_month,
        "days_remaining": days_remaining,
        "by_provider": by_provider,
    }


# ---------------------------------------------------------------------------
# 14.3  query_anomalies
# ---------------------------------------------------------------------------


def query_anomalies(
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
    lookback_days: int = 30,
    z_threshold: float = 2.0,
) -> dict[str, Any]:
    """Detect per-(provider, account, model_id) token spikes vs recent history.

    For each combination, pulls period_type=day rows covering the last
    lookback_days+1 days.  Today's row is the signal; the prior lookback_days
    rows are the historical baseline.  Emits an anomaly when:
      - z = (today_tokens - mean) / stdev > z_threshold
      - today is non-zero
      - historical stdev > 0 and n >= 2.
    """
    now = datetime.now(UTC)
    today_key = now.strftime("%Y-%m-%d")
    oldest_key = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == "day",
        UsagePeriodRollup.sidecar_id == "",  # all-sidecars grain
        UsagePeriodRollup.period_key >= oldest_key,
        UsagePeriodRollup.period_key <= today_key,
    )
    if provider_id:
        stmt = stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsagePeriodRollup.account_id == account_id)
    rows = list(session.exec(stmt).all())

    # Group by (provider_id, account_id, model_id)
    GroupKey = tuple[str, str, str]
    by_group: dict[GroupKey, dict[str, Any]] = {}
    for r in rows:
        key: GroupKey = (r.provider_id, r.account_id, r.model_id)
        group = by_group.setdefault(key, {"today": None, "history": []})
        tokens = (
            r.tokens_input
            + r.tokens_output
            + r.tokens_cache_read
            + r.tokens_cache_create
            + r.tokens_reasoning
        )
        if r.period_key == today_key:
            group["today"] = {"tokens": tokens, "cost_usd": r.cost_usd}
        else:
            group["history"].append(tokens)

    anomalies: list[dict[str, Any]] = []
    for (pid, aid, mid), group in sorted(by_group.items()):
        today_data = group["today"]
        if today_data is None:
            continue
        today_tokens = today_data["tokens"]
        if today_tokens == 0:
            continue

        history = group["history"]
        if len(history) < 2:
            continue

        try:
            mean = statistics.mean(history)
            stdev = statistics.stdev(history)  # sample stdev (n-1)
        except statistics.StatisticsError:
            continue

        if stdev == 0:
            continue  # constant history — no meaningful z-score

        z = (today_tokens - mean) / stdev
        if z > z_threshold:
            anomalies.append(
                {
                    "provider_id": pid,
                    "account_id": aid,
                    "model_id": mid,
                    "today_tokens": today_tokens,
                    "today_cost_usd": today_data["cost_usd"],
                    "historical_mean_tokens": round(mean, 2),
                    "historical_stddev_tokens": round(stdev, 2),
                    "z_score_tokens": round(z, 4),
                    "verdict": "spike",
                }
            )

    return {
        "as_of": now.isoformat(),
        "lookback_days": lookback_days,
        "z_threshold": z_threshold,
        "anomalies": anomalies,
    }


# ---------------------------------------------------------------------------
# 16  History queries (restored from event-sourced model)
# ---------------------------------------------------------------------------


def _card_metadata_lookup(
    session: Session,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Build a lookup from (provider_id, account_id, window_type, model_id) → card metadata.

    Reads all LatestUsage rows and parses card_json to extract service_name,
    limit_value, unit_type, account_label, pct_used, and window_type.
    Returns both empty-model_id rows (aggregate) and per-model rows.
    """
    from app.models.db import LatestUsage

    records = session.exec(select(LatestUsage)).all()
    lookup: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in records:
        try:
            card = json.loads(r.card_json) if isinstance(r.card_json, str) else r.card_json
        except (json.JSONDecodeError, TypeError):
            continue
        key = (r.provider_id, r.account_id, r.window_type, r.model_id or "")
        lookup[key] = {
            "service_name": card.get("service_name", r.provider_id),
            "limit_value": card.get("limit_value"),
            "unit_type": card.get("unit_type", "generic"),
            "account_label": card.get("account_label"),
            "pct_used": card.get("pct_used"),
            "used_value": card.get("used_value"),
        }
        # Also store an aggregate entry keyed by (provider_id, account_id,
        # window_type, "") so we can fall back for model-scoped rollups.
        agg_key = (r.provider_id, r.account_id, r.window_type, "")
        if agg_key not in lookup:
            lookup[agg_key] = lookup[key]
    return lookup


def query_history_raw(
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = 1.0,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Return flat time-series rows from usage_period_rollup, enriched with
    card metadata from latest_usage.

    For days <= 1: 15-minute buckets from raw usage_events.
    For days <= 7: 1-hour buckets from period_type='hour'.
    For days > 7: daily buckets from period_type='day'.

    Each row has: timestamp, provider_id, account_id, service_name, window_type,
    model_id, used_value, limit_value, unit_type, token_usage, msgs.
    """
    now = datetime.now(UTC)
    since = now - timedelta(days=days)

    if days <= 1:
        return _history_raw_from_events(
            session,
            provider_id=provider_id,
            account_id=account_id,
            since=since,
            now=now,
            limit=limit,
        )
    if days <= 7:
        return _history_raw_from_rollup(
            session,
            provider_id=provider_id,
            account_id=account_id,
            since=since,
            period_type="hour",
            limit=limit,
        )
    return _history_raw_from_rollup(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since,
        period_type="day",
        limit=limit,
    )


def _history_raw_from_events(
    session: Session,
    *,
    provider_id: str | None,
    account_id: str | None,
    since: datetime,
    now: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    """15-minute bucketed aggregation from usage_events for short time ranges."""
    since_str = since.strftime("%Y-%m-%d %H:%M:%S.%f")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")

    params: dict[str, Any] = {
        "since": since_str,
        "now": now_str,
    }
    filters = ["e.kind = 'message'"]
    if provider_id:
        filters.append("e.provider_id = :provider_id")
        params["provider_id"] = provider_id
    if account_id:
        filters.append("e.account_id = :account_id")
        params["account_id"] = account_id
    where = " AND ".join(filters)

    sql = text(
        f"""
        SELECT
            e.provider_id,
            e.account_id,
            e.model_id,
            CASE WHEN e.model_id IS NULL OR e.model_id = '' THEN 1 ELSE 0 END AS is_agg,
            (strftime('%s', e.ts) / 900) * 900 AS bucket_epoch,
            SUM(e.tokens_input)   AS tokens_input,
            SUM(e.tokens_output)  AS tokens_output,
            SUM(e.tokens_cache_read)  AS tokens_cache_read,
            SUM(e.tokens_cache_create) AS tokens_cache_create,
            SUM(e.tokens_reasoning) AS tokens_reasoning,
            SUM(e.cost_usd)       AS cost_usd,
            COUNT(*)              AS msgs
        FROM usage_events e
        WHERE {where}
          AND e.ts >= :since
          AND e.ts <= :now
        GROUP BY e.provider_id, e.account_id, e.model_id, bucket_epoch
        ORDER BY bucket_epoch ASC
    """
    )

    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]

    card_meta = _card_metadata_lookup(session)

    results: list[dict[str, Any]] = []
    for r in rows:
        pid, aid, mid, _, bucket_epoch, ti, to, tcr, tcc, tr, cost, msgs = r
        ts = datetime.fromtimestamp(bucket_epoch, tz=UTC)
        meta = _find_card_meta(card_meta, pid, aid, mid)

        token_total = (ti or 0) + (to or 0) + (tcr or 0) + (tcc or 0) + (tr or 0)
        unit_type = meta.get("unit_type", "tokens")
        used_value = _compute_used_value(meta, token_total, cost or 0.0, unit_type)

        results.append(
            {
                "timestamp": iso_utc(ts),
                "provider_id": pid,
                "account_id": aid,
                "service_name": meta.get("service_name", pid),
                "window_type": meta.get("window_type", "unknown"),
                "model_id": mid or "",
                "used_value": used_value,
                "limit_value": meta.get("limit_value"),
                "unit_type": unit_type,
                "token_usage": {
                    "input": ti or 0,
                    "output": to or 0,
                    "cache_read": tcr or 0,
                    "cache_create": tcc or 0,
                    "reasoning": tr or 0,
                    "total": token_total,
                },
                "msgs": msgs or 0,
                "cost_usd": cost or 0.0,
            }
        )

    return results[:limit]


def _history_raw_from_rollup(
    session: Session,
    *,
    provider_id: str | None,
    account_id: str | None,
    since: datetime,
    period_type: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Bucketed aggregation from usage_period_rollup for medium/long time ranges."""
    since_key = (
        since.strftime("%Y-%m-%dT%H") if period_type == "hour" else since.strftime("%Y-%m-%d")
    )

    stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == period_type,
        UsagePeriodRollup.period_key >= since_key,
        UsagePeriodRollup.sidecar_id == "",
    )
    if provider_id:
        stmt = stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsagePeriodRollup.account_id == account_id)
    stmt = stmt.order_by(UsagePeriodRollup.period_key)

    rows = list(session.exec(stmt).all())
    card_meta = _card_metadata_lookup(session)

    results: list[dict[str, Any]] = []
    for r in rows:
        token_total = (
            r.tokens_input
            + r.tokens_output
            + r.tokens_cache_read
            + r.tokens_cache_create
            + r.tokens_reasoning
        )
        meta = _find_card_meta(card_meta, r.provider_id, r.account_id, r.model_id)
        unit_type = meta.get("unit_type", "tokens")
        used_value = _compute_used_value(meta, token_total, r.cost_usd, unit_type)

        # Parse period_key to timestamp
        ts = _parse_period_key(r.period_key, period_type)
        if ts is None:
            continue

        results.append(
            {
                "timestamp": iso_utc(ts),
                "provider_id": r.provider_id,
                "account_id": r.account_id,
                "service_name": meta.get("service_name", r.provider_id),
                "window_type": meta.get("window_type", "unknown"),
                "model_id": r.model_id,
                "used_value": used_value,
                "limit_value": meta.get("limit_value"),
                "unit_type": unit_type,
                "token_usage": {
                    "input": r.tokens_input,
                    "output": r.tokens_output,
                    "cache_read": r.tokens_cache_read,
                    "cache_create": r.tokens_cache_create,
                    "reasoning": r.tokens_reasoning,
                    "total": token_total,
                },
                "msgs": r.msgs,
                "cost_usd": r.cost_usd,
            }
        )

    return results[:limit]


def _find_card_meta(
    card_meta: dict[tuple[str, str, str, str], dict[str, Any]],
    provider_id: str,
    account_id: str,
    model_id: str,
) -> dict[str, Any]:
    """Find matching card metadata, trying model-specific key first, then aggregate."""
    key = (provider_id, account_id, "unknown", model_id)
    if key in card_meta:
        return card_meta[key]
    # Try any window_type for this (provider, account, model)
    for (pid, aid, _wt, mid), meta in card_meta.items():
        if pid == provider_id and aid == account_id and mid == model_id:
            return meta
    # Fall back to aggregate for this (provider, account)
    key = (provider_id, account_id, "unknown", "")
    if key in card_meta:
        return card_meta[key]
    for (pid, aid, _wt, mid), meta in card_meta.items():
        if pid == provider_id and aid == account_id and mid == "":
            return meta
    return {}


def _compute_used_value(
    meta: dict[str, Any],
    token_total: int,
    cost_usd: float,
    unit_type: str,
) -> float | None:
    """Compute a used_value from card metadata and rollup data.

    For token/generic cards: used_value = token_total (absolute tokens consumed).
    For percent cards: used_value = pct_used from live card (the gauge position).
    For currency cards: used_value = cost_usd (spend in dollars).
    For requests/minutes: used_value = msgs or token_total respectively.
    """
    pct_used = meta.get("pct_used")
    if unit_type == "percent" and pct_used is not None:
        return pct_used
    if unit_type == "currency":
        return cost_usd
    if unit_type in ("tokens", "generic", "token"):
        return float(token_total)
    if unit_type == "requests":
        return float(meta.get("used_value", token_total) or token_total)
    # Fallback: token_total
    return float(token_total) if token_total else meta.get("used_value")


def _parse_period_key(key: str, period_type: str) -> datetime | None:
    """Parse a period_key into a UTC datetime."""
    try:
        if period_type == "hour":
            return datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=UTC)
        if period_type == "day":
            return datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=UTC)
        if period_type == "month":
            return datetime.strptime(key, "%Y-%m").replace(day=1, tzinfo=UTC)
        if period_type == "year":
            return datetime.strptime(key, "%Y").replace(month=1, day=1, tzinfo=UTC)
    except ValueError:
        pass
    return None


def query_history_grouped(
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = 1.0,
    limit: int = 500,
) -> dict[str, Any]:
    """Return grouped history data with averages and peaks.

    Groups raw time-series data into time buckets, then within each bucket
    groups by (provider_id, account_id). Each group has a windows array
    containing per-window-type and per-model breakdowns.

    Returns {averages: [...], peaks: [...]}.
    """
    raw = query_history_raw(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
        limit=max(limit * 3, 2000),
    )

    if not raw:
        return {"averages": [], "peaks": []}

    # Group raw rows by timestamp bucket, then by (provider_id, account_id)
    bucket_map: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {}
    for row in raw:
        ts = row["timestamp"]
        key = (row["provider_id"], row["account_id"])
        bucket_map.setdefault(ts, {}).setdefault(key, []).append(row)

    # Build averages and peaks
    averages: list[dict[str, Any]] = []
    peaks: list[dict[str, Any]] = []

    for ts in sorted(bucket_map.keys()):
        for (pid, aid), rows in sorted(bucket_map[ts].items()):
            # Get metadata from first row
            first = rows[0]
            account_label = first.get("account_label")

            # Build windows array from the rows
            windows = []
            for r in rows:
                window_entry: dict[str, Any] = {
                    "window": r.get("window_type", "unknown"),
                    "category": r.get("window_type", "unknown"),
                    "model_id": r.get("model_id", ""),
                    "value": r.get("used_value"),
                    "limit": r.get("limit_value"),
                    "unit": r.get("unit_type", "tokens"),
                    "token_usage": r.get("token_usage"),
                    "msgs": r.get("msgs", 0),
                }
                if r.get("cost_usd") is not None:
                    window_entry["cost_usd"] = r["cost_usd"]
                if r.get("by_model") is not None:
                    window_entry["by_model"] = r["by_model"]
                windows.append(window_entry)

            entry = {
                "timestamp": ts,
                "provider_id": pid,
                "account_id": aid,
                "account_label": account_label,
                "windows": windows,
            }
            averages.append(entry)

            # Peak entry: use max used_value from each window
            peak_windows = []
            for w in windows:
                peak_w = {**w}
                if w.get("value") is not None:
                    peak_w["value"] = w["value"]
                peak_windows.append(peak_w)
            peaks.append({**entry, "windows": peak_windows})

    return {"averages": averages, "peaks": peaks}


def query_history_deltas(
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = 1.0,
) -> dict[str, Any]:
    """Compute actual consumption deltas from usage_events within the time range.

    Unlike the old gauge-based approach (tracking counter resets with glitch
    filtering), the event-sourced model makes this trivial: just sum the events.
    """
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    since_str = since.strftime("%Y-%m-%d %H:%M:%S.%f")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")

    params: dict[str, Any] = {"since": since_str, "now": now_str}
    filters = ["kind = 'message'"]
    if provider_id:
        filters.append("provider_id = :provider_id")
        params["provider_id"] = provider_id
    if account_id:
        filters.append("account_id = :account_id")
        params["account_id"] = account_id
    where = " AND ".join(filters)

    # Token totals
    sql = text(
        f"""
        SELECT
            provider_id,
            SUM(tokens_input + tokens_output + tokens_cache_read + tokens_cache_create + tokens_reasoning) AS total_tokens,
            SUM(cost_usd) AS total_cost
        FROM usage_events
        WHERE {where}
          AND ts >= :since
          AND ts <= :now
        GROUP BY provider_id
    """
    )
    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]

    token_delta_total = 0
    cost_delta_total = 0.0
    provider_token_deltas: dict[str, float] = {}

    for r in rows:
        pid, tokens, cost = r
        t = int(tokens or 0)
        c = float(cost or 0.0)
        token_delta_total += t
        cost_delta_total += c
        provider_token_deltas[pid] = provider_token_deltas.get(pid, 0.0) + t

    # Critical series count from latest_usage cards
    from app.models.db import LatestUsage

    card_rows = session.exec(select(LatestUsage)).all()
    critical_series_count = 0
    for cr in card_rows:
        try:
            card = json.loads(cr.card_json) if isinstance(cr.card_json, str) else cr.card_json
        except (json.JSONDecodeError, TypeError):
            continue
        if provider_id and card.get("provider_id") != provider_id:
            continue
        if account_id and card.get("account_id") != account_id:
            continue
        pct = card.get("pct_used")
        if pct is not None and pct >= 90:
            critical_series_count += 1
        elif (
            card.get("used_value") is not None
            and card.get("limit_value")
            and card["limit_value"] > 0
        ):
            if (card["used_value"] / card["limit_value"]) >= 0.9:
                critical_series_count += 1

    return {
        "token_delta_total": float(token_delta_total),
        "cost_delta_total": round(cost_delta_total, 6),
        "provider_token_deltas": {k: round(v, 2) for k, v in provider_token_deltas.items()},
        "critical_series_count": critical_series_count,
        "series_sampled": False,
        "series": [],
    }
