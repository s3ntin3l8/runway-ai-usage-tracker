"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from app.models._datetime import iso_utc
from app.models.db import UsagePeriodRollup
from app.services.queries._shared import _parse_period_key


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
            "window_type": r.window_type,
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
                "account_label": meta.get("account_label"),
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
                "account_label": meta.get("account_label"),
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


def _find_all_card_metas(
    card_meta: dict[tuple[str, str, str, str], dict[str, Any]],
    provider_id: str,
    account_id: str,
    model_id: str,
) -> list[dict[str, Any]]:
    """Return all distinct-window-type card metas for (provider, account, model).

    Falls back to aggregate (model_id="") entries when no model-specific ones exist.
    """
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for (pid, aid, wt, mid), meta in card_meta.items():
        if pid == provider_id and aid == account_id and mid == model_id and wt not in seen:
            results.append(meta)
            seen.add(wt)
    if not results:
        for (pid, aid, wt, mid), meta in card_meta.items():
            if pid == provider_id and aid == account_id and mid == "" and wt not in seen:
                results.append(meta)
                seen.add(wt)
    return results


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
    if unit_type == "percent":
        return pct_used  # None when no live snapshot — caller keeps unit="percent" so frontend skips it
    if unit_type == "currency":
        return cost_usd
    if unit_type in ("tokens", "generic", "token"):
        return float(token_total)
    if unit_type == "requests":
        return float(meta.get("used_value", token_total) or token_total)
    # Fallback: token_total
    return float(token_total) if token_total else meta.get("used_value")


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

    # Card meta lookup needed to expand each event row into all matching quota windows.
    card_meta = _card_metadata_lookup(session)

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
            first = rows[0]
            account_label = first.get("account_label")

            # Expand each event row into one window entry per quota window type.
            # Events don't carry window_type, so we look up all card metas for
            # (provider, account, model) and generate a window entry per distinct
            # window_type. Dedup by (window_type, model_id) to avoid duplicates.
            windows: list[dict[str, Any]] = []
            seen_wm: set[tuple[str, str]] = set()
            for r in rows:
                mid = r.get("model_id", "")
                all_metas = _find_all_card_metas(card_meta, pid, aid, mid)
                if not all_metas:
                    all_metas = [{}]
                for meta in all_metas:
                    wt = meta.get("window_type", r.get("window_type", "unknown"))
                    if (wt, mid) in seen_wm:
                        continue
                    seen_wm.add((wt, mid))
                    unit = meta.get("unit_type", r.get("unit_type", "tokens"))
                    token_total = (r.get("token_usage") or {}).get("total", 0)
                    val = _compute_used_value(meta, token_total, r.get("cost_usd") or 0.0, unit)
                    window_entry: dict[str, Any] = {
                        "window": wt,
                        "category": wt,
                        "model_id": mid,
                        "value": val,
                        "limit": meta.get("limit_value"),
                        "unit": unit,
                        "token_usage": r.get("token_usage"),
                        "msgs": r.get("msgs", 0),
                        "cost_usd": r.get("cost_usd"),
                    }
                    windows.append(window_entry)

            entry = {
                "timestamp": ts,
                "provider_id": pid,
                "account_id": aid,
                "account_label": account_label,
                "windows": windows,
            }
            averages.append(entry)

            peaks.append({**entry, "windows": [{**w} for w in windows]})

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

    # Token totals — cache-inclusive to match global-stats / top-models / chart
    # bars / cumulative. cache_tokens is returned as a separate split so the UI
    # can honor the exclude-cache toggle.
    sql = text(
        f"""
        SELECT
            provider_id,
            SUM(tokens_input + tokens_output + tokens_reasoning
                + tokens_cache_read + tokens_cache_create) AS total_tokens,
            SUM(tokens_cache_read + tokens_cache_create) AS cache_tokens,
            SUM(cost_usd) AS total_cost,
            SUM(cost_cache_read + cost_cache_create) AS cache_cost
        FROM usage_events
        WHERE {where}
          AND ts >= :since
          AND ts <= :now
        GROUP BY provider_id
    """
    )
    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]

    token_delta_total = 0
    token_cache_total = 0
    cost_delta_total = 0.0
    cost_cache_total = 0.0
    provider_token_deltas: dict[str, float] = {}

    for r in rows:
        pid, tokens, cache_tokens, cost, cache_cost = r
        t = int(tokens or 0)
        cache = int(cache_tokens or 0)
        c = float(cost or 0.0)
        cc = float(cache_cost or 0.0)
        token_delta_total += t
        token_cache_total += cache
        cost_delta_total += c
        cost_cache_total += cc
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
        "token_cache_total": float(token_cache_total),
        "cost_delta_total": round(cost_delta_total, 6),
        "cost_cache_total": round(cost_cache_total, 6),
        "provider_token_deltas": {k: round(v, 2) for k, v in provider_token_deltas.items()},
        "critical_series_count": critical_series_count,
        "series_sampled": False,
        "series": [],
    }


# ---------------------------------------------------------------------------
# Window-first history queries
# ---------------------------------------------------------------------------
