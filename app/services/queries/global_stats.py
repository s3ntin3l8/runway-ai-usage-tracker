"""Global, cross-provider usage snapshot for the Insights strip.

Every other aggregation is scoped to one ``(provider_id, account_id)``; this
answers the "all of it, everywhere" headline questions: lifetime totals, how
many distinct models/providers, overall cache savings, session economics, and
when activity peaks. Totals + distinct counts come from the pre-aggregated
``usage_period_rollup`` lifetime rows (fast, tz-irrelevant); session economics
and the busiest-hour histogram come from ``usage_events``.
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session, select

from app.models.db import UsagePeriodRollup


def query_global_stats(session: Session, *, tz: ZoneInfo | None = None) -> dict[str, Any]:
    """Return a single global snapshot across all providers/accounts.

    Shape::

        {
          "lifetime": {tokens_total, tokens_cache, cost_usd, cost_cache, msgs,
                       tokens_input, tokens_output, tokens_cache_read,
                       tokens_cache_create, tokens_reasoning},
          "sessions": {count, avg_cost, avg_tokens},
          "cache_hit_ratio": float,       # cache_read / all tokens, 0..1
          "distinct_models": int,
          "distinct_providers": int,
          "busiest_day":  {period_key: "YYYY-MM-DD", tokens} | None,   # UTC date
          "busiest_hour": {hour: 0-23, tokens} | None,                 # local tz
          "generated_at": ISO-8601 UTC,
        }
    """
    # --- Lifetime totals + distinct counts (rollup, all-sidecar grain) -------
    life_rows = list(
        session.exec(
            select(UsagePeriodRollup).where(
                UsagePeriodRollup.period_type == "lifetime",
                UsagePeriodRollup.sidecar_id == "",
            )
        ).all()
    )

    lifetime = {
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_cache_read": 0,
        "tokens_cache_create": 0,
        "tokens_reasoning": 0,
        "cost_usd": 0.0,
        "cost_cache": 0.0,
        "msgs": 0,
    }
    models: set[str] = set()
    providers: set[str] = set()
    for r in life_rows:
        providers.add(r.provider_id)
        if r.model_id:
            # Per-model grain row — only used to count distinct models. Adding
            # its tokens would double-count against the all-up (model_id="") row.
            models.add(r.model_id)
            continue
        lifetime["tokens_input"] += r.tokens_input
        lifetime["tokens_output"] += r.tokens_output
        lifetime["tokens_cache_read"] += r.tokens_cache_read
        lifetime["tokens_cache_create"] += r.tokens_cache_create
        lifetime["tokens_reasoning"] += r.tokens_reasoning
        lifetime["cost_usd"] += r.cost_usd
        lifetime["cost_cache"] += r.cost_cache_read + r.cost_cache_create
        lifetime["msgs"] += r.msgs

    tokens_total = (
        lifetime["tokens_input"]
        + lifetime["tokens_output"]
        + lifetime["tokens_cache_read"]
        + lifetime["tokens_cache_create"]
        + lifetime["tokens_reasoning"]
    )
    lifetime["tokens_total"] = tokens_total
    lifetime["tokens_cache"] = lifetime["tokens_cache_read"] + lifetime["tokens_cache_create"]
    cache_hit_ratio = (lifetime["tokens_cache_read"] / tokens_total) if tokens_total > 0 else 0.0

    # --- Session economics (events; per-session subquery so session-less API
    #     events never skew the averages) -------------------------------------
    sess_row = session.exec(  # type: ignore[call-overload]
        text(
            """
            SELECT
                COUNT(*)        AS session_count,
                AVG(s_tokens)   AS avg_tokens,
                AVG(s_cost)     AS avg_cost
            FROM (
                SELECT
                    SUM(tokens_input + tokens_output + tokens_cache_read
                        + tokens_cache_create + tokens_reasoning) AS s_tokens,
                    SUM(cost_usd)                                 AS s_cost
                FROM usage_events
                WHERE kind = 'message'
                  AND session_id IS NOT NULL
                GROUP BY provider_id, account_id, session_id
            )
            """
        )
    ).first()
    sessions = {
        "count": int(sess_row.session_count or 0) if sess_row else 0,
        "avg_tokens": float(sess_row.avg_tokens or 0.0) if sess_row else 0.0,
        "avg_cost": float(sess_row.avg_cost or 0.0) if sess_row else 0.0,
    }

    # --- Busiest day (rollup day grain, all-up; UTC calendar date) -----------
    day_row = session.exec(  # type: ignore[call-overload]
        text(
            """
            SELECT period_key,
                   SUM(tokens_input + tokens_output + tokens_cache_read
                       + tokens_cache_create + tokens_reasoning) AS tokens
            FROM usage_period_rollup
            WHERE period_type = 'day' AND model_id = '' AND sidecar_id = ''
            GROUP BY period_key
            ORDER BY tokens DESC
            LIMIT 1
            """
        )
    ).first()
    busiest_day = (
        {"period_key": day_row.period_key, "tokens": int(day_row.tokens or 0)}
        if day_row and (day_row.tokens or 0) > 0
        else None
    )

    # --- Busiest hour (rollup hour grain; UTC hour-start re-bucketed to local
    #     tz so "peak hour" matches the user's wall clock) -------------------
    busiest_hour = _busiest_hour_local(session, tz)

    return {
        "lifetime": lifetime,
        "sessions": sessions,
        "cache_hit_ratio": round(cache_hit_ratio, 4),
        "distinct_models": len(models),
        "distinct_providers": len(providers),
        "busiest_day": busiest_day,
        "busiest_hour": busiest_hour,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _busiest_hour_local(session: Session, tz: ZoneInfo | None) -> dict[str, int] | None:
    """Sum hour-grain rollups into 24 local-tz buckets, return the peak.

    Each rollup hour row is keyed by its UTC hour start (``YYYY-MM-DDTHH``);
    we attribute the whole hour's tokens to the local hour that UTC instant
    falls in. Approximate for sub-hour-offset zones, but cheap (no event scan)
    and correct to the hour for the common whole-hour offsets.
    """
    rows = session.exec(  # type: ignore[call-overload]
        text(
            """
            SELECT period_key,
                   SUM(tokens_input + tokens_output + tokens_cache_read
                       + tokens_cache_create + tokens_reasoning) AS tokens
            FROM usage_period_rollup
            WHERE period_type = 'hour' AND model_id = '' AND sidecar_id = ''
            GROUP BY period_key
            """
        )
    ).all()

    buckets: dict[int, int] = {}
    for r in rows:
        tokens = int(r.tokens or 0)
        if tokens <= 0:
            continue
        try:
            dt = datetime.strptime(r.period_key, "%Y-%m-%dT%H").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        hour = dt.astimezone(tz).hour if tz else dt.hour
        buckets[hour] = buckets.get(hour, 0) + tokens

    if not buckets:
        return None
    hour, tokens = max(buckets.items(), key=lambda kv: kv[1])
    return {"hour": hour, "tokens": tokens}
