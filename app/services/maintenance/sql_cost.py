"""Shared per-event SQL cost expression.

``fallback_priced_events`` uses this per-event cost arithmetic
(``compute_event_cost_breakdown``'s total). ``stale_priced_models`` keeps its own
bucket-sum estimate for its perf/query-count contract, but shares this module's
expression if it ever moves to per-event detection. Keeping a single copy here
prevents the two from drifting.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, func, literal
from sqlmodel import col

from app.models.db import UsageEvent
from app.services.cost_calculator import PricingRow


def sql_cost(row: PricingRow) -> Any:
    """SQL expression for ``compute_event_cost_breakdown``'s total at ``row``,
    evaluated per event. The row's rates are literals, so one aggregate query
    can apply the exact per-event rule to every event of a price interval (a
    day-level sum would hide events that match next to events that do not)."""
    prompt = (
        col(UsageEvent.tokens_input)
        + col(UsageEvent.tokens_cache_read)
        + col(UsageEvent.tokens_cache_create)
    )
    is_aggregate = and_(
        func.substr(UsageEvent.event_id, 1, 6) == "cloud:",
        func.instr(UsageEvent.event_id, ":m:") == 0,
    )
    has_long = (
        row.long_prompt_threshold_tokens is not None
        and row.long_input_per_mtok is not None
        and row.long_output_per_mtok is not None
    )
    long_cond = and_(~is_aggregate, prompt > row.long_prompt_threshold_tokens) if has_long else None

    def rate(base: float, long: float | None) -> Any:
        if long_cond is None or long is None:
            return literal(base)
        return case((long_cond, literal(long)), else_=literal(base))

    def opt(long: float | None, base: float) -> float | None:
        return base if long is None else long

    rate_5m_std = row.cache_create_per_mtok
    rate_1h_std = row.cache_create_1h_per_mtok or rate_5m_std
    rate_5m_long = opt(row.long_cache_create_per_mtok, rate_5m_std)
    rate_1h_long = (
        opt(row.long_cache_create_1h_per_mtok, row.cache_create_1h_per_mtok) or rate_5m_long
    )
    split = col(UsageEvent.tokens_cache_create_1h) + col(UsageEvent.tokens_cache_create_5m)
    cache_5m_tokens = case(
        (
            split > 0,
            UsageEvent.tokens_cache_create_5m + func.max(UsageEvent.tokens_cache_create - split, 0),
        ),
        else_=UsageEvent.tokens_cache_create,
    )
    return (
        UsageEvent.tokens_input * rate(row.input_per_mtok, row.long_input_per_mtok)
        + (UsageEvent.tokens_output + UsageEvent.tokens_reasoning)
        * rate(row.output_per_mtok, row.long_output_per_mtok)
        + UsageEvent.tokens_cache_read * rate(row.cache_read_per_mtok, row.long_cache_read_per_mtok)
        + UsageEvent.tokens_cache_create_1h * rate(rate_1h_std, rate_1h_long)
        + cache_5m_tokens * rate(rate_5m_std, rate_5m_long)
    ) / 1_000_000
