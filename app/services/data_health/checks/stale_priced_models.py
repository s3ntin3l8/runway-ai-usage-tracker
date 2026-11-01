"""Find models whose stored cost estimate disagrees with today's price table.

Pricing corrections (a new model row that events used to reach through a
family fallback, a cache-read rate that was seeded wrong) only reach stored
events through a recost. `unpriced_models` handles events whose stored cost is
$0; this check handles the ones that carry a non-zero but outdated
`cost_estimated_usd`.

It is deliberately not a full re-price. The fix (`plan_recost`/`apply_recost`
in `estimate_only` mode) refreshes only the computed estimate and its cost
breakdown, evaluated at each event's own timestamp, so a real price change
(a new row with a newer `effective_from`) is never applied to older events. It
changes `cost_usd` only where that already equals the old estimate and never
touches `cost_reported_usd`, so reported costs and a `billing_type` change since
ingest keep their history.

`detect` is a cheap hint: one SQL aggregate over the message events (token sums
per price-relevant bucket) compared with the estimate those sums price at today,
never a per-event Python pass. `plan` runs the exact per-event computation for
one `(provider, model)` group.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import case, func
from sqlalchemy import select as sa_select
from sqlmodel import Session, col

from app.models.db import UsageEvent
from app.services.cost_calculator import (
    PricingIndex,
    PricingRow,
    PricingRowSnapshot,
    resolve_price_row,
)
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
from app.services.maintenance._retry import DATA_HEALTH_LOCK_PROFILE
from app.services.maintenance.recost import (
    apply_recost,
    is_priced_on_purpose_free,
    plan_recost,
)
from app.services.maintenance.sql_cost import sql_cost as _sql_cost

_KEY_SEP = "::"
# A group is flagged when the recomputed estimate is off by more than this
# (absolute dollars or fraction of the stored estimate, whichever is larger).
_MIN_DELTA_USD = 0.01
_MIN_DELTA_FRACTION = 0.001


@dataclass
class _Bucket:
    events: int = 0
    stored: float = 0.0
    expected: float = 0.0
    first_ts: str | None = None
    last_ts: str | None = None
    stale_events: int = 0


def _group_key(provider_id: str, model_id: str) -> str:
    return f"{provider_id}{_KEY_SEP}{model_id}"


def _split_key(group_key: str) -> tuple[str, str]:
    provider_id, sep, model_id = group_key.partition(_KEY_SEP)
    if not sep or not provider_id or not model_id:
        raise ValueError(f"malformed stale_priced_models group key: {group_key!r}")
    return provider_id, model_id


def _expected_estimate(
    row: PricingRowSnapshot | Any,
    *,
    aggregate: bool,
    long_flag: bool,
    tokens_input: int,
    tokens_output: int,
    tokens_cache_read: int,
    cache_create_1h: int,
    cache_create_5m_all: int,
) -> float:
    """The estimate `compute_event_cost_breakdown` would produce, applied to
    bucket sums. The cost is linear in each token dimension within a bucket of
    events sharing a price row and tier, so the sum of per-event estimates
    equals the estimate of the sums (up to per-event rounding)."""
    rate_in = row.input_per_mtok
    rate_out = row.output_per_mtok
    rate_read = row.cache_read_per_mtok
    rate_5m = row.cache_create_per_mtok
    rate_1h = row.cache_create_1h_per_mtok
    if (
        not aggregate
        and long_flag
        and row.long_input_per_mtok is not None
        and row.long_output_per_mtok is not None
    ):
        rate_in = row.long_input_per_mtok
        rate_out = row.long_output_per_mtok
        if row.long_cache_read_per_mtok is not None:
            rate_read = row.long_cache_read_per_mtok
        if row.long_cache_create_per_mtok is not None:
            rate_5m = row.long_cache_create_per_mtok
        if row.long_cache_create_1h_per_mtok is not None:
            rate_1h = row.long_cache_create_1h_per_mtok
    rate_1h = rate_1h or rate_5m
    return (
        tokens_input * rate_in
        + tokens_output * rate_out
        + tokens_cache_read * rate_read
        + cache_create_1h * rate_1h
        + cache_create_5m_all * rate_5m
    ) / 1_000_000


def _price_intervals(
    session: Session, index: PricingIndex
) -> list[tuple[str, str, date, date, PricingRow]]:
    """Runs of consecutive event days that resolve to the same price row."""
    day = func.date(UsageEvent.ts)
    stmt = (
        sa_select(UsageEvent.provider_id, UsageEvent.model_id, day)  # type: ignore[call-overload]
        .where(
            UsageEvent.kind == "message",
            UsageEvent.cost_usd != 0.0,
            UsageEvent.cost_estimated_usd > 0,
            col(UsageEvent.model_id).is_not(None),
        )
        .group_by(UsageEvent.provider_id, UsageEvent.model_id, day)
        .order_by(UsageEvent.provider_id, UsageEvent.model_id, day)
    )
    intervals: list[tuple[str, str, date, date, PricingRow]] = []
    provider_dates: dict[str, list[date]] = {}
    for provider_id, model_id, day_text in session.execute(stmt):
        assert model_id is not None
        if is_priced_on_purpose_free(provider_id, model_id):
            continue
        on = date.fromisoformat(day_text)
        if provider_id not in provider_dates:
            provider_dates[provider_id] = index.effective_dates(provider_id)
        bisect_right(provider_dates[provider_id], on)
        ts = datetime.combine(on, time.min, tzinfo=UTC)
        row = resolve_price_row(session, provider_id, model_id, ts, index=index)
        if row is None:
            continue
        last = intervals[-1] if intervals else None
        if (
            last
            and last[:2] == (provider_id, model_id)
            and last[4] == row
            and (last[3] + timedelta(days=1)) == on
        ):
            intervals[-1] = (provider_id, model_id, last[2], on, row)
        else:
            intervals.append((provider_id, model_id, on, on, row))
    return intervals


def _aggregate_buckets(session: Session) -> dict[tuple[str, str], _Bucket]:
    index = PricingIndex.load(session)
    intervals = _price_intervals(session, index)
    buckets: dict[tuple[str, str], _Bucket] = defaultdict(_Bucket)
    for provider_id, model_id, first_day, last_day, row in intervals:
        via_today = _sql_cost(row)
        # Per-event split handling mirrors compute_event_cost_breakdown.
        split = col(UsageEvent.tokens_cache_create_1h) + col(UsageEvent.tokens_cache_create_5m)
        cache_create_5m_tokens = case(
            (
                split > 0,
                UsageEvent.tokens_cache_create_5m
                + func.max(UsageEvent.tokens_cache_create - split, 0),
            ),
            else_=UsageEvent.tokens_cache_create,
        )
        # long condition per event
        pass
        stmt = sa_select(  # type: ignore[call-overload]
            func.count(),
            func.sum(UsageEvent.tokens_input),
            func.sum(UsageEvent.tokens_output + UsageEvent.tokens_reasoning),
            func.sum(UsageEvent.tokens_cache_read),
            func.sum(UsageEvent.tokens_cache_create_1h),
            func.sum(cache_create_5m_tokens),
            func.sum(UsageEvent.cost_estimated_usd),
            func.min(UsageEvent.ts),
            func.max(UsageEvent.ts),
            func.sum(via_today),
            func.sum(
                case((func.abs(via_today - UsageEvent.cost_estimated_usd) > 2e-6, 1), else_=0)
            ),
        ).where(
            col(UsageEvent.provider_id) == provider_id,
            col(UsageEvent.model_id) == model_id,
            col(UsageEvent.kind) == "message",
            col(UsageEvent.cost_usd) != 0.0,
            col(UsageEvent.cost_estimated_usd) > 0,
            col(UsageEvent.ts)
            >= datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC),
            col(UsageEvent.ts)
            < datetime(last_day.year, last_day.month, last_day.day, tzinfo=UTC) + timedelta(days=1),
        )
        row_result = session.execute(stmt).one()
        (
            events,
            tokens_input,
            tokens_output,
            tokens_cache_read,
            cache_create_1h,
            cache_create_5m_total,
            stored,
            first_ts,
            last_ts,
            expected,
            stale_count,
        ) = row_result
        if not events:
            continue
        # expected via bucket sums at row rates (linear, so equals sum of per-event)
        # Use _expected_estimate with agg=False? We need per-event aggregate/long mix.
        # Compute expected as sum over events using base rates; long-tier events
        # use long rates. Approximate: use the row's base rates for the whole
        # interval (the previous code split by long flag). To preserve accuracy,
        # instead compute expected per-event via sql_cost sum:
        stored = stored or 0.0
        expected = expected or 0.0
        bucket = buckets[(provider_id, model_id)]
        bucket.events += events
        bucket.stored += stored
        bucket.expected += expected
        bucket.stale_events += stale_count or 0
        first_ts_text, last_ts_text = str(first_ts), str(last_ts)
        if bucket.first_ts is None or first_ts_text < bucket.first_ts:
            bucket.first_ts = first_ts_text
        if bucket.last_ts is None or last_ts_text > bucket.last_ts:
            bucket.last_ts = last_ts_text
    return buckets


class StalePricedModelsCheck(Check):
    id = "stale_priced_models"
    title = "Some stored cost estimates use an outdated rate"
    description = (
        "A model's stored cost estimate differs from what today's price table gives at "
        "each event's own date, for example after a pricing correction or a new model row."
    )
    impact = (
        "Cost totals for these models are over- or understated until the estimates are refreshed."
    )
    recommended_action = (
        "Review each model's preview, then refresh its estimates. Reported costs and "
        "price changes dated after an event are not applied to it."
    )
    severity = Severity.WARN
    blocked_by = ("legacy_provider_ids",)

    def detect(self, session: Session) -> CheckReport:
        groups: list[FindingGroup] = []
        for (provider_id, model_id), bucket in sorted(_aggregate_buckets(session).items()):
            delta = bucket.expected - bucket.stored
            if abs(delta) <= max(_MIN_DELTA_USD, _MIN_DELTA_FRACTION * abs(bucket.stored)):
                continue
            detail = {
                "provider_id": provider_id,
                "model_id": model_id,
                "events": bucket.events,
                "first_ts": bucket.first_ts,
                "last_ts": bucket.last_ts,
                "stored_usd": round(bucket.stored, 4),
                "recomputed_usd": round(bucket.expected, 4),
                "delta_usd": round(delta, 4),
            }
            groups.append(
                FindingGroup(
                    key=_group_key(provider_id, model_id),
                    label=(
                        f"{provider_id}/{model_id}: up to {bucket.stale_events} event(s) priced at an "
                        f"outdated rate ({delta:+.2f} USD)"
                    ),
                    count=bucket.stale_events,
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
        provider_id, model_id = _split_key(group_key)
        recost_plan = plan_recost(
            session, [provider_id], estimate_only=True, models=[model_id], sample_size=0
        )
        delta = recost_plan.new_estimate_sum - recost_plan.stored_estimate_sum
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Refresh the cost estimate of {recost_plan.updated + recost_plan.zeroed} "
                f"{provider_id}/{model_id} event(s)"
            ),
            counts={
                "updated": recost_plan.updated,
                # A corrected row whose rates are all zero, on an event that
                # displayed the estimate: its shown cost goes to $0.
                "zeroed": recost_plan.zeroed,
                "unchanged": recost_plan.unchanged,
                "skipped_unpopulated": recost_plan.skipped_unpopulated,
                "events_keeping_cost_usd": recost_plan.kept_cost_usd,
                "stored_estimate_usd": round(recost_plan.stored_estimate_sum, 4),
                "new_estimate_usd": round(recost_plan.new_estimate_sum, 4),
                "delta_usd": round(delta, 4),
            },
            confirmation_text=(
                f"Refresh {provider_id}/{model_id} estimates ({delta:+.2f} USD)? Reported "
                "costs and price changes dated after an event are not applied to it. "
                "Back up the database first."
            ),
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        provider_id, model_id = _split_key(group_key)
        result = apply_recost(
            session,
            [provider_id],
            estimate_only=True,
            models=[model_id],
            lock_profile=DATA_HEALTH_LOCK_PROFILE,
        )
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Refreshed the cost estimate of {provider_id}/{model_id}",
                counts={
                    "updated": result.updated,
                    "zeroed": result.zeroed,
                    "unchanged": result.unchanged,
                    "skipped_unpopulated": result.skipped_unpopulated,
                    "rollups_rebuilt_pairs": result.rollups_rebuilt_pairs,
                    "windows_rebuilt": result.windows_rebuilt,
                },
            ),
            [],
        )
