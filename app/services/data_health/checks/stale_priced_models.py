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
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import and_, case, func, literal
from sqlalchemy import select as sa_select
from sqlmodel import Session, col

from app.models.db import UsageEvent
from app.services.cost_calculator import PricingIndex, PricingRowSnapshot, resolve_price_row
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
from app.services.maintenance.recost import (
    apply_recost,
    is_priced_on_purpose_free,
    plan_recost,
)

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


def _aggregate_buckets(session: Session) -> dict[tuple[str, str], _Bucket]:
    index = PricingIndex.load(session)
    thresholds = index.long_thresholds()

    prompt = (
        col(UsageEvent.tokens_input)
        + col(UsageEvent.tokens_cache_read)
        + col(UsageEvent.tokens_cache_create)
    )
    split = col(UsageEvent.tokens_cache_create_1h) + col(UsageEvent.tokens_cache_create_5m)
    is_aggregate = and_(
        func.substr(UsageEvent.event_id, 1, 6) == "cloud:",
        func.instr(UsageEvent.event_id, ":m:") == 0,
    )
    day = func.date(UsageEvent.ts).label("day")
    aggregate_flag = case((is_aggregate, 1), else_=0).label("agg")
    long_flags = [
        case((prompt > literal(t), 1), else_=0).label(f"long_{i}") for i, t in enumerate(thresholds)
    ]
    # Per-event split handling mirrors compute_event_cost_breakdown: an event
    # with a 1h/5m split prices its unsplit remainder at the 5m rate, one
    # without a split prices the whole total there.
    cache_create_5m_all = func.sum(
        case(
            (
                split > 0,
                UsageEvent.tokens_cache_create_5m
                + func.max(UsageEvent.tokens_cache_create - split, 0),
            ),
            else_=UsageEvent.tokens_cache_create,
        )
    )
    stmt = (
        sa_select(  # type: ignore[call-overload]
            UsageEvent.provider_id,
            UsageEvent.model_id,
            day,
            aggregate_flag,
            *long_flags,
            func.count(),
            func.sum(UsageEvent.tokens_input),
            func.sum(UsageEvent.tokens_output + UsageEvent.tokens_reasoning),
            func.sum(UsageEvent.tokens_cache_read),
            func.sum(UsageEvent.tokens_cache_create_1h),
            cache_create_5m_all,
            func.sum(UsageEvent.cost_estimated_usd),
            func.min(UsageEvent.ts),
            func.max(UsageEvent.ts),
        )
        .where(
            UsageEvent.kind == "message",
            UsageEvent.cost_usd != 0.0,
            # Events with no stored estimate (legacy rows) are left alone.
            UsageEvent.cost_estimated_usd > 0,
            col(UsageEvent.model_id).is_not(None),
        )
        .group_by(
            UsageEvent.provider_id,
            UsageEvent.model_id,
            day,
            aggregate_flag,
            *long_flags,
        )
    )
    buckets: dict[tuple[str, str], _Bucket] = defaultdict(_Bucket)
    price_rows: dict[tuple[str, str, int], Any] = {}
    provider_dates: dict[str, list[date]] = {}
    n_flags = len(long_flags)
    for provider_id, model_id, day_text, agg, *rest in session.execute(stmt):
        flags = rest[:n_flags]
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
        ) = rest[n_flags:]
        assert model_id is not None
        if is_priced_on_purpose_free(provider_id, model_id):
            continue
        # Resolution is constant between two of the provider's effective_from
        # dates, so resolve once per interval (also keeps the fallback warnings
        # in resolve_price_row to once per model and interval, not per day).
        day_value = date.fromisoformat(day_text)
        if provider_id not in provider_dates:
            provider_dates[provider_id] = index.effective_dates(provider_id)
        dates = provider_dates[provider_id]
        interval = bisect_right(dates, day_value)
        cache_key = (provider_id, model_id, interval)
        if cache_key not in price_rows:
            ts = datetime.combine(day_value, time.min, tzinfo=UTC)
            price_rows[cache_key] = resolve_price_row(
                session, provider_id, model_id, ts, index=index
            )
        row = price_rows[cache_key]
        if row is None:
            continue  # no rate resolves: nothing to refresh an estimate from
        threshold = row.long_prompt_threshold_tokens
        long_flag = bool(threshold is not None and flags and flags[thresholds.index(threshold)])
        expected = _expected_estimate(
            row,
            aggregate=bool(agg),
            long_flag=long_flag,
            tokens_input=tokens_input or 0,
            tokens_output=tokens_output or 0,
            tokens_cache_read=tokens_cache_read or 0,
            cache_create_1h=cache_create_1h or 0,
            cache_create_5m_all=cache_create_5m_total or 0,
        )
        stored = stored or 0.0
        bucket = buckets[(provider_id, model_id)]
        bucket.events += events
        bucket.stored += stored
        bucket.expected += expected
        first_ts_text, last_ts_text = str(first_ts), str(last_ts)
        if bucket.first_ts is None or first_ts_text < bucket.first_ts:
            bucket.first_ts = first_ts_text
        if bucket.last_ts is None or last_ts_text > bucket.last_ts:
            bucket.last_ts = last_ts_text
        # Per-event rounding (six decimals, four components) bounds the
        # noise a clean bucket can show.
        if abs(expected - stored) > events * 2e-6 + 1e-6:
            bucket.stale_events += events
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
        result = apply_recost(session, [provider_id], estimate_only=True, models=[model_id])
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
