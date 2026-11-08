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

`detect` is a cheap hint: one SQL aggregate per contiguous-day price interval,
applying the per-event rule (via the shared sql_cost expression) in SQL, never
a per-event Python pass. `plan` runs the exact per-event computation for
one `(provider, model)` group.
"""

from __future__ import annotations

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
from app.services.maintenance.recost import (
    apply_recost,
    is_priced_on_purpose_free,
    plan_recost,
)
from app.services.maintenance.retry import DATA_HEALTH_LOCK_PROFILE
from app.services.maintenance.sql_cost import sql_cost as _sql_cost

_KEY_SEP = "::"
# A group is flagged when the recomputed estimate is off by more than this
# (absolute dollars or fraction of the stored estimate, whichever is larger),
# or when any individual event's stored estimate differs from today's rate
# beyond the per-event tolerance.
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
    for provider_id, model_id, day_text in session.execute(stmt):
        assert model_id is not None
        if is_priced_on_purpose_free(provider_id, model_id):
            continue
        on = date.fromisoformat(day_text)
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
        stmt = sa_select(  # type: ignore[call-overload]
            func.count(),
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
            stored,
            first_ts,
            last_ts,
            expected,
            stale_count,
        ) = row_result
        if not events:
            continue
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
            if bucket.stale_events == 0 and abs(delta) <= max(
                _MIN_DELTA_USD, _MIN_DELTA_FRACTION * abs(bucket.stored)
            ):
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
            _suffix = f" ({delta:+.2f} USD)" if abs(delta) >= 0.005 else ""
            groups.append(
                FindingGroup(
                    key=_group_key(provider_id, model_id),
                    label=(
                        f"{provider_id}/{model_id}: up to {bucket.stale_events} event(s) priced at an "
                        f"outdated rate{_suffix}"
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
        _delta_text = f" ({delta:+.2f} USD)" if abs(delta) >= 0.005 else ""
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
                f"Refresh {provider_id}/{model_id} estimates{_delta_text}? Reported "
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
