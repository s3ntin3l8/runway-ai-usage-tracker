"""Recompute usage_events.cost_usd (and derived rollups/windows) after a
provider_pricing seed change — the shared logic behind `scripts/recost_events.py`
and the Data Health `unpriced_models` fix.

Cost resolution itself lives in `event_cost.resolve_event_cost`, the single
rule ingest and recost now share (see that module's docstring for the two
bugs unifying it fixed). This module adds the event-scanning, legacy
reported-cost derivation, and rollup/window rebuild orchestration around it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlmodel import Session, col, select

from app.models.db import ProviderConfig, UsageEvent
from app.services.maintenance.event_cost import ResolvedCost, resolve_event_cost
from app.services.maintenance.windows import rebuild_windows_for_providers
from app.services.period_rollups import rebuild_rollups_for_pairs

_COST_FIELDS = ("cost_usd", "cost_reported_usd", "cost_estimated_usd")


@dataclass(frozen=True)
class RecostChange:
    event_id: int
    provider_id: str
    account_id: str
    event_ref: str  # event_id column, for display
    old_cost_usd: float
    resolved: ResolvedCost


@dataclass
class RecostPlan:
    updated: int = 0
    unchanged: int = 0
    zeroed: int = 0
    skipped_still_unpriced: int = 0
    affected_pairs: set[tuple[str, str]] = field(default_factory=set)
    samples: list[RecostChange] = field(default_factory=list)


@dataclass
class RecostResult(RecostPlan):
    rollups_rebuilt_pairs: int = 0
    windows_rebuilt: int = 0


def _event_scope(providers: list[str] | None, since: date | None, only_zero_cost: bool):
    stmt = select(UsageEvent).where(UsageEvent.kind == "message")
    if providers:
        stmt = stmt.where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
    if since:
        stmt = stmt.where(UsageEvent.ts >= datetime(since.year, since.month, since.day, tzinfo=UTC))
    if only_zero_cost:
        stmt = stmt.where(UsageEvent.cost_usd == 0.0)
    return stmt.order_by(col(UsageEvent.ts))


def _legacy_reported_cost(ev: UsageEvent) -> float | None:
    """What `reported_cost` should be for an event that may predate the
    `cost_reported_usd` column: OpenCode's legacy backends logged their
    subscription amount straight into `cost_usd`, so an event with no
    `cost_reported_usd` yet gets that value read back as its report."""
    if ev.cost_reported_usd is not None:
        return ev.cost_reported_usd
    if ev.provider_id.startswith("opencode"):
        return ev.cost_usd
    return None


@dataclass(frozen=True)
class RecostSkip:
    reason: str  # "unchanged" | "still_unpriced"


def _compute_changes(
    session: Session,
    providers: list[str] | None,
    since: date | None,
    only_zero_cost: bool,
) -> Iterator[RecostChange | RecostSkip]:
    """Yields a RecostChange per event that needs a write, or a skip-reason
    string for one that doesn't — callers that only need the changes can
    filter with `isinstance(x, RecostChange)`; plan_recost also tallies the
    skip reasons for an accurate preview count."""
    configs = {
        (c.provider_id, c.account_id): c.billing_type
        for c in session.exec(select(ProviderConfig)).all()
    }
    for ev in session.exec(_event_scope(providers, since, only_zero_cost)):
        billing_type = configs.get((ev.provider_id, ev.account_id), "unknown")
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
            billing_type=billing_type,
            reported_cost=_legacy_reported_cost(ev),
        )
        if only_zero_cost and resolved.cost_usd <= ev.cost_usd:
            # only_zero_cost is the Data Health "give an unpriced model a
            # price" fixer's contract: it never lowers an existing cost, and
            # skips a row that's still zero after resolving (still unpriced).
            yield RecostSkip("still_unpriced")
            continue
        changed = (
            abs(resolved.cost_usd - ev.cost_usd) > 1e-9
            or abs(resolved.cost_estimated_usd - ev.cost_estimated_usd) > 1e-9
            or resolved.cost_reported_usd != ev.cost_reported_usd
        )
        if not changed:
            yield RecostSkip("unchanged")
            continue
        yield RecostChange(
            event_id=ev.id,  # type: ignore[arg-type]
            provider_id=ev.provider_id,
            account_id=ev.account_id,
            event_ref=ev.event_id,
            old_cost_usd=ev.cost_usd,
            resolved=resolved,
        )


def plan_recost(
    session: Session,
    providers: list[str] | None,
    *,
    since: date | None = None,
    only_zero_cost: bool = False,
    sample_size: int = 20,
) -> RecostPlan:
    """Read-only preview — no writes, no commit."""
    plan = RecostPlan()
    for item in _compute_changes(session, providers, since, only_zero_cost):
        if isinstance(item, RecostSkip):
            if item.reason == "unchanged":
                plan.unchanged += 1
            else:
                plan.skipped_still_unpriced += 1
            continue
        plan.affected_pairs.add((item.provider_id, item.account_id))
        if item.resolved.cost_usd == 0.0:
            plan.zeroed += 1
        else:
            plan.updated += 1
        if len(plan.samples) < sample_size:
            plan.samples.append(item)
    return plan


def apply_recost(
    session: Session,
    providers: list[str] | None,
    *,
    since: date | None = None,
    only_zero_cost: bool = False,
    skip_rollups: bool = False,
    skip_windows: bool = False,
) -> RecostResult:
    """Recompute cost_usd for events in scope, then rebuild the rollups and
    windows for every affected (provider_id, account_id) pair. Commits as it
    goes (batched every 1000 events, matching the prior script's behavior).
    """
    result = RecostResult()
    write_count = 0
    for item in _compute_changes(session, providers, since, only_zero_cost):
        if isinstance(item, RecostSkip):
            if item.reason == "unchanged":
                result.unchanged += 1
            else:
                result.skipped_still_unpriced += 1
            continue
        ev = session.get(UsageEvent, item.event_id)
        assert ev is not None  # loaded by the same scoped query moments ago
        ev.cost_usd = item.resolved.cost_usd
        ev.cost_reported_usd = item.resolved.cost_reported_usd
        ev.cost_estimated_usd = item.resolved.cost_estimated_usd
        ev.cost_input = item.resolved.breakdown.input
        ev.cost_output = item.resolved.breakdown.output
        ev.cost_cache_read = item.resolved.breakdown.cache_read
        ev.cost_cache_create = item.resolved.breakdown.cache_create
        session.add(ev)
        result.affected_pairs.add((item.provider_id, item.account_id))
        if item.resolved.cost_usd == 0.0:
            result.zeroed += 1
        else:
            result.updated += 1
        write_count += 1
        if write_count % 1000 == 0:
            session.commit()
    session.commit()

    if not skip_rollups and result.affected_pairs:
        rebuild_rollups_for_pairs(session, result.affected_pairs)
        session.commit()
        result.rollups_rebuilt_pairs = len(result.affected_pairs)

    if not skip_windows and result.affected_pairs:
        touched_providers = sorted({p for p, _a in result.affected_pairs})
        result.windows_rebuilt = rebuild_windows_for_providers(session, touched_providers)

    return result
