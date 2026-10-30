"""Re-price old events that were billed through the family fallback.

Until a model got a price row of its own, `resolve_price_row` billed it at the
version-suffix family row (`sonnet-5` at `sonnet`) or a segment-trim sibling.
Events ingested before the `cost_estimated_usd` column existed carry only a
`cost_usd` that came from that fallback and no stored estimate, so neither
`unpriced_models` (only $0 events) nor `stale_priced_models` (only events with
a populated estimate) ever corrects them.

The rule is evidence based, not a full recost. An event is re-priced only when
all of these hold:

- `kind='message'`, `cost_estimated_usd = 0`, `cost_usd > 0` and
  `cost_reported_usd IS NULL` (nothing the source reported is ever rewritten);
- the model has an EXACT price row in effect at the event's own date today;
- a fallback row exists for the model (the row `resolve_price_row` would have
  used without the model's own row), the cost computed at that fallback row is
  within 2% of the stored `cost_usd`, and the cost at today's row differs from
  it. That proves the event was billed through the fallback; an event matching
  neither (or only today's row) is left alone.

Such an event gets `cost_usd`, `cost_estimated_usd` and the four cost
components from today's row. `cost_reported_usd` is never written. Rollups and
windows are rebuilt for the affected `(provider, account)` pairs, like
`apply_recost`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, or_, update
from sqlmodel import Session, col, select

from app.models.db import UsageEvent
from app.services.cost_calculator import (
    _VERSION_SUFFIX,
    PricingIndex,
    PricingRowSnapshot,
    compute_event_cost_breakdown,
    is_aggregate_event_id,
)
from app.services.maintenance.event_cost import ResolvedCost, resolve_event_cost
from app.services.maintenance.windows import rebuild_windows_for_providers
from app.services.model_normalization import is_free_tier_model_id
from app.services.period_rollups import rebuild_rollups_for_pairs

_EVENT_BATCH = 1000

# An event matches the fallback when its stored cost is this close to it.
FALLBACK_TOLERANCE = 0.02
# Today's cost counts as "different from the stored one" beyond this.
_SAME_COST_EPSILON = 1e-9


def fallback_price_row(
    index: PricingIndex, provider_id: str, model_id: str, effective_on: date
) -> PricingRowSnapshot | None:
    """The row `resolve_price_row` would bill `model_id` at if the model had no
    row of its own: the version-suffix family row, then the progressively
    segment-trimmed sibling. `None` when neither exists (or for a free-tier id,
    which `resolve_price_row` never lets reach a paid sibling).

    Deliberately mirrors the two fallbacks in `resolve_price_row` (without the
    warning logs, and without the final case-insensitive match of the model's
    own id); `tests/unit/test_fallback_priced_events.py` pins the two together."""
    if not model_id or is_free_tier_model_id(model_id):
        return None
    row: PricingRowSnapshot | None = None
    family = _VERSION_SUFFIX.sub("", model_id)
    if family != model_id:
        row = index.exact(provider_id, family, effective_on)
    if row is None:
        segments = model_id.split("-")
        while row is None and len(segments) > 1:
            segments.pop()
            row = index.exact(provider_id, "-".join(segments), effective_on)
    return row


def group_key(provider_id: str, model_id: str) -> str:
    return f"{provider_id}::{model_id}"


def split_group_key(key: str) -> tuple[str, str]:
    provider_id, sep, model_id = key.partition("::")
    if not sep or not provider_id or not model_id:
        raise ValueError(f"malformed fallback_priced_events group key: {key!r}")
    return provider_id, model_id


def legacy_event_filters(provider_id: str | None = None, model_id: str | None = None) -> list[Any]:
    """The SQL half of the rule, shared by detect and the per-event scan."""
    filters: list[Any] = [
        UsageEvent.kind == "message",
        UsageEvent.cost_estimated_usd == 0,
        UsageEvent.cost_usd > 0,
        col(UsageEvent.cost_reported_usd).is_(None),
        col(UsageEvent.model_id).is_not(None),
    ]
    if provider_id is not None:
        filters.append(UsageEvent.provider_id == provider_id)
    if model_id is not None:
        filters.append(UsageEvent.model_id == model_id)
    return filters


@dataclass(frozen=True)
class _Candidate:
    id: int
    provider_id: str
    account_id: str
    event_id: str
    ts: datetime
    model_id: str
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_create: int
    tokens_reasoning: int
    tokens_cache_create_1h: int
    tokens_cache_create_5m: int
    cost_usd: float


@dataclass(frozen=True)
class FallbackChange:
    event_id: int
    provider_id: str
    account_id: str
    old_cost_usd: float
    resolved: ResolvedCost


@dataclass
class FallbackPlan:
    events: int = 0
    stored_usd: float = 0.0
    new_usd: float = 0.0
    # Scanned events the rule left alone, by reason.
    no_exact_row: int = 0
    no_fallback_row: int = 0
    not_billed_through_fallback: int = 0
    affected_pairs: set[tuple[str, str]] = field(default_factory=set)

    @property
    def delta_usd(self) -> float:
        return self.new_usd - self.stored_usd


@dataclass
class FallbackResult(FallbackPlan):
    rollups_rebuilt_pairs: int = 0
    windows_rebuilt: int = 0


def _candidate_pages(session: Session, provider_id: str, model_id: str) -> Iterator[_Candidate]:
    # Keyset pagination like recost._event_pages: no live cursor across the
    # periodic commits, and re-priced events leaving the scope (their estimate
    # becomes non-zero) cannot make an OFFSET skip rows.
    max_id = session.execute(select(func.max(UsageEvent.id))).scalar_one()
    if max_id is None:
        return
    columns = [getattr(UsageEvent, name) for name in _Candidate.__dataclass_fields__]
    scope = (
        select(*columns)
        .where(*legacy_event_filters(provider_id, model_id), UsageEvent.id <= max_id)  # type: ignore[arg-type]
        .order_by(col(UsageEvent.ts), col(UsageEvent.id))
    )
    cursor: tuple[datetime, int] | None = None
    while True:
        stmt = scope
        if cursor is not None:
            ts, row_id = cursor
            stmt = stmt.where(
                col(UsageEvent.ts) >= ts,
                or_(
                    col(UsageEvent.ts) > ts,
                    (col(UsageEvent.ts) == ts) & (col(UsageEvent.id) > row_id),
                ),
            )
        rows = session.execute(stmt.limit(_EVENT_BATCH)).all()
        if not rows:
            return
        for values in rows:
            ev = _Candidate(*values)
            cursor = (ev.ts, ev.id)
            yield ev


def _breakdown_total(
    session: Session, ev: _Candidate, row: PricingRowSnapshot, *, standard: bool
) -> float:
    return compute_event_cost_breakdown(
        session,
        provider_id=ev.provider_id,
        model_id=ev.model_id,
        ts=ev.ts,
        tokens_input=ev.tokens_input,
        tokens_output=ev.tokens_output,
        tokens_cache_read=ev.tokens_cache_read,
        tokens_cache_create=ev.tokens_cache_create,
        tokens_reasoning=ev.tokens_reasoning,
        tokens_cache_create_1h=ev.tokens_cache_create_1h,
        tokens_cache_create_5m=ev.tokens_cache_create_5m,
        standard_tier_only=standard,
        _resolved_price_row=row,
        _price_row_resolved=True,
    ).total


def _scan(session: Session, provider_id: str, model_id: str) -> Iterator[FallbackChange | str]:
    """A `FallbackChange` per event to re-price, or a skip reason string."""
    index = PricingIndex.load(session, [provider_id])
    for ev in _candidate_pages(session, provider_id, model_id):
        on = ev.ts.date()
        today = index.exact(ev.provider_id, ev.model_id, on)
        if today is None:
            yield "no_exact_row"
            continue
        fallback = fallback_price_row(index, ev.provider_id, ev.model_id, on)
        if fallback is None:
            yield "no_fallback_row"
            continue
        standard = is_aggregate_event_id(ev.event_id)
        via_fallback = _breakdown_total(session, ev, fallback, standard=standard)
        via_today = _breakdown_total(session, ev, today, standard=standard)
        if (
            abs(via_fallback - ev.cost_usd) > FALLBACK_TOLERANCE * ev.cost_usd
            or abs(via_today - ev.cost_usd) <= _SAME_COST_EPSILON
        ):
            yield "not_billed_through_fallback"
            continue
        resolved = resolve_event_cost(
            session,
            provider_id=ev.provider_id,
            model_id=ev.model_id,
            ts=ev.ts,
            tokens_input=ev.tokens_input,
            tokens_output=ev.tokens_output,
            tokens_cache_read=ev.tokens_cache_read,
            tokens_cache_create=ev.tokens_cache_create,
            tokens_reasoning=ev.tokens_reasoning,
            tokens_cache_create_1h=ev.tokens_cache_create_1h,
            tokens_cache_create_5m=ev.tokens_cache_create_5m,
            billing_type="unknown",
            reported_cost=None,
            standard_tier_only=standard,
            resolved_price_row=today,
            price_row_resolved=True,
        )
        yield FallbackChange(
            event_id=ev.id,
            provider_id=ev.provider_id,
            account_id=ev.account_id,
            old_cost_usd=ev.cost_usd,
            resolved=resolved,
        )


def _tally(plan: FallbackPlan, item: FallbackChange | str) -> bool:
    """Fold one scan item into the counters; True when it is a change."""
    if isinstance(item, str):
        if item == "no_exact_row":
            plan.no_exact_row += 1
        elif item == "no_fallback_row":
            plan.no_fallback_row += 1
        elif item == "not_billed_through_fallback":
            plan.not_billed_through_fallback += 1
        else:
            raise ValueError(f"unknown skip reason: {item!r}")
        return False
    plan.events += 1
    plan.stored_usd += item.old_cost_usd
    plan.new_usd += item.resolved.cost_usd
    plan.affected_pairs.add((item.provider_id, item.account_id))
    return True


def plan_fallback_priced_events(session: Session, provider_id: str, model_id: str) -> FallbackPlan:
    """Read-only preview for one `(provider, model)` group: counts and dollars."""
    plan = FallbackPlan()
    for item in _scan(session, provider_id, model_id):
        _tally(plan, item)
    return plan


def apply_fallback_priced_events(
    session: Session,
    provider_id: str,
    model_id: str,
    *,
    skip_rollups: bool = False,
    skip_windows: bool = False,
) -> FallbackResult:
    """Re-price the group's matching events, commit in batches, then rebuild
    rollups and windows for the affected pairs. Idempotent: a re-priced event
    carries a stored estimate and so leaves the rule's scope."""
    result = FallbackResult()
    writes: list[dict[str, Any]] = []
    for item in _scan(session, provider_id, model_id):
        if not _tally(result, item):
            continue
        assert isinstance(item, FallbackChange)
        resolved = item.resolved
        writes.append(
            {
                "id": item.event_id,
                "cost_usd": resolved.cost_usd,
                "cost_estimated_usd": resolved.cost_estimated_usd,
                "cost_input": resolved.breakdown.input,
                "cost_output": resolved.breakdown.output,
                "cost_cache_read": resolved.breakdown.cache_read,
                "cost_cache_create": resolved.breakdown.cache_create,
            }
        )
        if len(writes) == _EVENT_BATCH:
            session.execute(update(UsageEvent), writes)
            session.commit()
            writes.clear()
    if writes:
        session.execute(update(UsageEvent), writes)
    session.commit()

    if not skip_rollups and result.affected_pairs:
        rebuild_rollups_for_pairs(session, result.affected_pairs)
        session.commit()
        result.rollups_rebuilt_pairs = len(result.affected_pairs)
    if not skip_windows and result.affected_pairs:
        result.windows_rebuilt = rebuild_windows_for_providers(
            session, sorted({p for p, _a in result.affected_pairs})
        )
    return result
