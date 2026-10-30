"""Find old events that were billed through the family fallback.

See `app/services/maintenance/fallback_priced_events.py` for the rule. `detect`
is a cheap hint: one SQL aggregate per price interval (a run of event days that
resolve to the same exact and fallback rows) applies the exact per-event rule in
SQL, with both rows' rates as literals, and sums the matching events. No
per-event Python pass. `plan` runs the per-event rule in Python for one
`(provider, model)` group.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, literal
from sqlalchemy import select as sa_select
from sqlmodel import Session, col

from app.models.db import UsageEvent
from app.services.cost_calculator import PricingIndex, PricingRowSnapshot
from app.services.data_health.base import (
    AsyncHook,
    Check,
    CheckReport,
    Finding,
    FindingGroup,
    FixPlan,
    FixResult,
    Severity,
)
from app.services.maintenance.fallback_priced_events import (
    FALLBACK_TOLERANCE,
    apply_fallback_priced_events,
    fallback_price_row,
    legacy_event_filters,
    plan_fallback_priced_events,
    split_group_key,
)
from app.services.maintenance.fallback_priced_events import (
    group_key as make_group_key,
)


@dataclass
class _Bucket:
    events: int = 0
    stored: float = 0.0
    new: float = 0.0
    first_ts: str | None = None
    last_ts: str | None = None


def _sql_cost(row: PricingRowSnapshot) -> Any:
    """SQL expression for `compute_event_cost_breakdown`'s total at `row`,
    evaluated per event. The row's rates are literals, so one aggregate
    query can apply the exact per-event rule to every event of a price
    interval (a day-level sum would hide events that match next to events
    that do not)."""
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


def _price_ranges(
    session: Session,
) -> list[tuple[str, str, date, date, PricingRowSnapshot, PricingRowSnapshot]]:
    """`(provider, model, first_day, last_day, today_row, fallback_row)` for
    every run of consecutive event days that resolve to the same pair of rows
    (only models with both an exact and a fallback row appear). Rows are
    constant between two `effective_from` dates, so a few ranges cover a model."""
    index = PricingIndex.load(session)
    day = func.date(UsageEvent.ts)
    stmt = (
        sa_select(UsageEvent.provider_id, UsageEvent.model_id, day)  # type: ignore[call-overload]
        .where(*legacy_event_filters())
        .group_by(UsageEvent.provider_id, UsageEvent.model_id, day)
        .order_by(UsageEvent.provider_id, UsageEvent.model_id, day)
    )
    ranges: list[tuple[str, str, date, date, PricingRowSnapshot, PricingRowSnapshot]] = []
    for provider_id, model_id, day_text in session.execute(stmt):
        on = date.fromisoformat(day_text)
        today = index.exact(provider_id, model_id, on)
        fallback = fallback_price_row(index, provider_id, model_id, on) if today else None
        if today is None or fallback is None:
            continue
        last = ranges[-1] if ranges else None
        if last and last[:2] == (provider_id, model_id) and last[4:] == (today, fallback):
            ranges[-1] = (*last[:3], on, today, fallback)
        else:
            ranges.append((provider_id, model_id, on, on, today, fallback))
    return ranges


def _aggregate_buckets(session: Session) -> dict[tuple[str, str], _Bucket]:
    buckets: dict[tuple[str, str], _Bucket] = defaultdict(_Bucket)
    for provider_id, model_id, first_day, last_day, today, fallback in _price_ranges(session):
        via_today = _sql_cost(today)
        via_fallback = _sql_cost(fallback)
        stmt = sa_select(  # type: ignore[call-overload]
            func.count(),
            func.sum(UsageEvent.cost_usd),
            func.sum(via_today),
            func.min(UsageEvent.ts),
            func.max(UsageEvent.ts),
        ).where(
            *legacy_event_filters(provider_id, model_id),
            # Half-open range on `ts` itself, so the (kind, ts) index can serve it.
            col(UsageEvent.ts)
            >= datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC),
            col(UsageEvent.ts)
            < datetime(last_day.year, last_day.month, last_day.day, tzinfo=UTC) + timedelta(days=1),
            # Per-event rule; the six-decimal rounding of the stored cost is
            # covered by a small absolute allowance.
            func.abs(via_fallback - UsageEvent.cost_usd)
            <= FALLBACK_TOLERANCE * UsageEvent.cost_usd + 2e-6,
            func.abs(via_today - UsageEvent.cost_usd) > 2e-6,
        )
        events, stored, new, first_ts, last_ts = session.execute(stmt).one()
        if not events:
            continue
        bucket = buckets[(provider_id, model_id)]
        bucket.events += events
        bucket.stored += stored or 0.0
        bucket.new += new or 0.0
        first_text, last_text = str(first_ts), str(last_ts)
        if bucket.first_ts is None or first_text < bucket.first_ts:
            bucket.first_ts = first_text
        if bucket.last_ts is None or last_text > bucket.last_ts:
            bucket.last_ts = last_text
    return buckets


class FallbackPricedEventsCheck(Check):
    id = "fallback_priced_events"
    title = "Old usage was billed through a family price"
    description = (
        "Events from before a model had its own price row were costed at its family "
        "row and never re-priced. Their stored cost matches that family rate, not "
        "today's price for the model."
    )
    impact = "Cost totals for these models are over- or understated for the affected period."
    recommended_action = (
        "Review each model's preview, then re-price its events at today's rate. "
        "Reported costs and events whose cost matches neither rate are not changed."
    )
    severity = Severity.WARN
    blocked_by = ("legacy_provider_ids",)

    def detect(self, session: Session) -> CheckReport:
        groups: list[FindingGroup] = []
        for (provider_id, model_id), bucket in sorted(_aggregate_buckets(session).items()):
            delta = bucket.new - bucket.stored
            detail = {
                "provider_id": provider_id,
                "model_id": model_id,
                "events": bucket.events,
                "first_ts": bucket.first_ts,
                "last_ts": bucket.last_ts,
                "stored_usd": round(bucket.stored, 4),
                "new_usd": round(bucket.new, 4),
                "delta_usd": round(delta, 4),
            }
            groups.append(
                FindingGroup(
                    key=make_group_key(provider_id, model_id),
                    label=(
                        f"{provider_id}/{model_id}: {bucket.events} event(s) billed through a "
                        f"family price ({delta:+.2f} USD)"
                    ),
                    count=bucket.events,
                    fixable=True,
                    samples=[Finding(label=f"{provider_id}/{model_id}", detail=detail)],
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=Severity.WARN if groups else Severity.INFO,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        provider_id, model_id = split_group_key(group_key)
        plan = plan_fallback_priced_events(session, provider_id, model_id)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=f"Re-price {plan.events} {provider_id}/{model_id} event(s) at today's rate",
            counts={
                "events": plan.events,
                "stored_usd": round(plan.stored_usd, 4),
                "new_usd": round(plan.new_usd, 4),
                "delta_usd": round(plan.delta_usd, 4),
                "left_alone": plan.not_billed_through_fallback
                + plan.no_exact_row
                + plan.no_fallback_row,
            },
            confirmation_text=(
                f"Re-price {plan.events} {provider_id}/{model_id} event(s) "
                f"({plan.delta_usd:+.2f} USD)? Reported costs are not changed. "
                "Back up the database first."
            ),
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id, model_id = split_group_key(group_key)
        result = apply_fallback_priced_events(session, provider_id, model_id)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Re-priced {result.events} {provider_id}/{model_id} event(s)",
                counts={
                    "events": result.events,
                    "stored_usd": round(result.stored_usd, 4),
                    "new_usd": round(result.new_usd, 4),
                    "delta_usd": round(result.delta_usd, 4),
                    "rollups_rebuilt_pairs": result.rollups_rebuilt_pairs,
                    "windows_rebuilt": result.windows_rebuilt,
                },
            ),
            [],
        )
