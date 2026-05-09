"""Read-side query helpers for the event-sourced usage data model.

Provides four query functions consumed by the /api/v1/usage/{events,
window-history, heatmap, sessions} endpoints, plus cost forecast and
anomaly detection helpers for the §12 optional enhancements.
"""

import calendar
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, text
from sqlmodel import Session, select

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
        select(
            UsageEvent.model_id,
            UsageEvent.sidecar_id,
            func.count(UsageEvent.id),
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
