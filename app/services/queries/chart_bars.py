"""Token and cost bar charts for `query_chart`.

Hourly or daily rollup bars, partial-day boundary events, per-model
double-count skipping, and optional provider summing. Moved verbatim
out of `snapshots.query_chart`.
"""

from datetime import UTC, datetime, time, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlmodel import Session, select

from app.models.db import UsagePeriodRollup
from app.models.utc_datetime import iso_utc
from app.services.queries.shared import parse_period_key, sqlite_utc_timestamp


def query_chart_bars(
    session: Session,
    *,
    provider_id: str | None,
    account_id: str | None,
    days: float,
    metric: str,
    since: datetime,
    until: datetime | None,
    group: str | None,
    sidecar_id: str | None,
    explicit_range: bool,
) -> dict:
    # tokens or cost — hourly bars for short rolling windows, daily bars
    # otherwise. A scoped range (explicit since/until, e.g. a whole month) always
    # renders as daily bars.
    period_type = "day" if (explicit_range or days > 7) else "hour"
    since_key = (
        since.strftime("%Y-%m-%dT%H") if period_type == "hour" else since.strftime("%Y-%m-%d")
    )
    bar_stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == period_type,
        UsagePeriodRollup.sidecar_id == (sidecar_id or ""),
    )
    boundary_ranges: list[tuple[datetime, datetime]] = []
    bar_until: datetime | None = until
    if period_type == "day" and explicit_range:
        since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        until_utc = (
            (until.astimezone(UTC) if until.tzinfo else until.replace(tzinfo=UTC))
            if until
            else datetime.now(UTC)
        )
        bar_until = until_utc
        since_day = datetime.combine(since_utc.date(), time.min, tzinfo=UTC)
        until_day = datetime.combine(until_utc.date(), time.min, tzinfo=UTC)
        full_start = since_day if since_utc == since_day else since_day + timedelta(days=1)
        since_key = full_start.strftime("%Y-%m-%d")
        # A partial UTC day cannot use its whole-day rollup: that would include
        # usage outside [since, until). Use exact raw events for that day instead.
        if since_utc < full_start:
            boundary_ranges.append((since_utc, min(full_start, until_utc)))
        # Include the end day when it is partial, including when it is also the
        # first day (until_day == full_start for a midnight start).
        if until_utc > until_day >= full_start:
            boundary_ranges.append((max(until_day, since_utc), until_utc))
    else:
        since_key = (
            since.strftime("%Y-%m-%dT%H") if period_type == "hour" else since.strftime("%Y-%m-%d")
        )
    bar_stmt = bar_stmt.where(UsagePeriodRollup.period_key >= since_key)
    if bar_until is not None:
        # Complete daily rollups stop at the UTC midnight containing `until`;
        # the partial end day is aggregated from raw events below.
        until_key = (
            bar_until.strftime("%Y-%m-%dT%H")
            if period_type == "hour"
            else bar_until.strftime("%Y-%m-%d")
        )
        bar_stmt = bar_stmt.where(UsagePeriodRollup.period_key < until_key)
    if provider_id:
        bar_stmt = bar_stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        bar_stmt = bar_stmt.where(UsagePeriodRollup.account_id == account_id)

    all_bar_rows = list(session.exec(bar_stmt.order_by(UsagePeriodRollup.period_key)).all())
    boundary_rows: list[dict[str, Any]] = []
    if boundary_ranges:
        event_ranges = [
            f"(ts >= :boundary_since_{i} AND ts < :boundary_until_{i})"
            for i in range(len(boundary_ranges))
        ]
        params: dict[str, Any] = {
            f"boundary_since_{i}": sqlite_utc_timestamp(start)
            for i, (start, _) in enumerate(boundary_ranges)
        }
        params.update(
            {
                f"boundary_until_{i}": sqlite_utc_timestamp(end)
                for i, (_, end) in enumerate(boundary_ranges)
            }
        )
        params["provider_id"] = provider_id
        params["account_id"] = account_id
        if sidecar_id:
            params["sidecar_id"] = sidecar_id
        provider_clause = "AND provider_id = :provider_id" if provider_id else ""
        account_clause = "AND account_id = :account_id" if account_id else ""
        sidecar_clause = "AND sidecar_id = :sidecar_id" if sidecar_id else ""
        # These f-strings interpolate only server-built SQL clauses above;
        # provider, account, and timestamp values remain bound parameters.
        # Like the rollup's empty sidecar_id, this event query covers all sidecars.
        sql = text(
            f"""
            SELECT provider_id, account_id, model_id,
                   strftime('%Y-%m-%d', ts) AS period_key,
                   SUM(tokens_input) AS tokens_input,
                   SUM(tokens_output) AS tokens_output,
                   SUM(tokens_cache_read) AS tokens_cache_read,
                   SUM(tokens_cache_create) AS tokens_cache_create,
                   SUM(tokens_reasoning) AS tokens_reasoning,
                   SUM(cost_usd) AS cost_usd,
                   SUM(cost_cache_read) AS cost_cache_read,
                   SUM(cost_cache_create) AS cost_cache_create,
                   COUNT(*) AS msgs
            FROM usage_events
            WHERE kind = 'message'
              {provider_clause}
              {account_clause}
              {sidecar_clause}
              AND ({" OR ".join(event_ranges)})
            GROUP BY provider_id, account_id, model_id, period_key
            """
        )
        boundary_rows = [
            dict(row._mapping)
            for row in session.exec(sql, params=params).all()  # type: ignore[call-overload]
        ]
    # (provider, account, period) tuples that have per-model rows — used to skip
    # their aggregate (model_id="") row so it isn't double-counted. Keyed by
    # account too: an account's aggregate row must survive even when a *sibling*
    # account of the same provider has per-model rows (matters cross-provider /
    # unfiltered, where rows from multiple accounts share a bar).
    has_per_model: set[tuple[str, str, str]] = {
        (r.provider_id, r.account_id, r.period_key) for r in all_bar_rows if r.model_id != ""
    }
    has_per_model.update(
        (r["provider_id"], r["account_id"], r["period_key"]) for r in boundary_rows if r["model_id"]
    )

    by_provider = group == "provider"
    bars_map: dict[str, list] = {}
    # When grouping by provider, segments accumulate per (period, provider) so
    # multiple accounts/models of one provider sum into a single bar segment.
    provider_seg: dict[tuple[str, str], dict[str, object]] = {}
    normalized_rows = [
        {
            "provider_id": r.provider_id,
            "account_id": r.account_id,
            "period_key": r.period_key,
            "model_id": r.model_id,
            "tokens_input": r.tokens_input,
            "tokens_output": r.tokens_output,
            "tokens_cache_read": r.tokens_cache_read,
            "tokens_cache_create": r.tokens_cache_create,
            "tokens_reasoning": r.tokens_reasoning,
            "cost_usd": r.cost_usd,
            "cost_cache_read": r.cost_cache_read,
            "cost_cache_create": r.cost_cache_create,
        }
        for r in all_bar_rows
    ] + boundary_rows
    for r in normalized_rows:
        if (
            not r["model_id"]
            and (r["provider_id"], r["account_id"], r["period_key"]) in has_per_model
        ):
            continue

        use_model = r["model_id"] or ""
        key = r["period_key"]
        if key not in bars_map:
            bars_map[key] = []
        value = (
            (r["cost_usd"] or 0)
            if metric == "cost"
            else (r["tokens_input"] or 0)
            + (r["tokens_output"] or 0)
            + (r["tokens_cache_read"] or 0)
            + (r["tokens_cache_create"] or 0)
            + (r["tokens_reasoning"] or 0)
        )
        # value_cache = the cache portion of `value`, so the client can subtract it
        # under the exclude-cache toggle. Its unit follows the metric: cache *tokens*
        # for the tokens bars, cache *cost* (USD) for the cost bars.
        value_cache = (
            (r["cost_cache_read"] or 0) + (r["cost_cache_create"] or 0)
            if metric == "cost"
            else (r["tokens_cache_read"] or 0) + (r["tokens_cache_create"] or 0)
        )

        if by_provider:
            seg_key = (key, r["provider_id"])
            seg = provider_seg.get(seg_key)
            if seg is None:
                seg = {
                    "provider_id": r["provider_id"],
                    "model_id": "",
                    "label": r["provider_id"].capitalize(),
                    "value": 0.0,
                    "value_cache": 0.0,
                }
                provider_seg[seg_key] = seg
                bars_map[key].append(seg)
            seg["value"] = cast(float, seg["value"]) + value
            seg["value_cache"] = cast(float, seg["value_cache"]) + value_cache
            continue

        label = r["provider_id"].capitalize()
        if use_model:
            label += f" · {use_model}"
        segment: dict[str, object] = {
            "provider_id": r["provider_id"],
            "model_id": use_model,
            "label": label,
            "value": value,
            "value_cache": value_cache,
        }
        bars_map[key].append(segment)

    bars = []
    for key in sorted(bars_map.keys()):
        ts = parse_period_key(key, period_type)
        bars.append(
            {
                "date": ts.strftime("%Y-%m-%d") if ts else key[:10],
                "ts": iso_utc(ts) if ts else key + ":00:00+00:00",
                "segments": bars_map[key],
            }
        )
    return {"bars": bars}
