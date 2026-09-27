"""Move usage_events from one account to another for a provider — the
Data Health `lone_default_events` fixer, `scripts/assign_default_events.py`
(explicit event ids), and `scripts/collapse_default_account_events.py`
(every event under a stale account) all share this.

Cross-account duplicate events (the same message ingested under two
different account_ids) can no longer exist on any database that has booted
a build carrying the `(provider_id, event_id)` unique index — see
`app/services/event_identity_migration.py`, which collapses any pre-existing
duplicates of that shape at startup. `collapse_default_account_events.py`'s
older pair-collapse/tie-break logic is therefore unreachable in production
today; this module only implements the still-live case, retagging events
that sit alone under the stale account_id.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, col, select, update

from app.models.db import UsageEvent
from app.services.maintenance.windows import rebuild_windows_overlapping
from app.services.period_rollups import rebuild_rollups_for_pairs


@dataclass
class ReassignPlan:
    count: int = 0
    ts_min: object = None
    ts_max: object = None
    samples: list[UsageEvent] = field(default_factory=list)


@dataclass
class ReassignResult:
    moved: int = 0
    rollups_rebuilt_pairs: int = 0
    windows_rebuilt: int = 0


def _scope(provider_id: str, source: str, event_ids: list[str] | None):
    stmt = select(UsageEvent).where(
        UsageEvent.provider_id == provider_id, UsageEvent.account_id == source
    )
    if event_ids:
        stmt = stmt.where(UsageEvent.event_id.in_(event_ids))  # type: ignore[attr-defined]
    return stmt


def plan_reassign_default(
    session: Session,
    *,
    provider_id: str,
    source: str,
    target: str,
    event_ids: list[str] | None = None,
    sample_size: int = 20,
) -> ReassignPlan:
    """Read-only preview. Raises ValueError if `event_ids` names an id that
    doesn't currently exist under `source` for this provider (matches
    `assign_default_events.py`'s original validation).
    """
    events = list(session.exec(_scope(provider_id, source, event_ids)))
    if event_ids and len(events) != len(set(event_ids)):
        found = {e.event_id for e in events}
        missing = sorted(set(event_ids) - found)
        raise ValueError(
            f"{len(missing)} event id(s) not found under {provider_id}/{source}: {missing}"
        )
    if not events:
        return ReassignPlan()
    return ReassignPlan(
        count=len(events),
        ts_min=min(e.ts for e in events),
        ts_max=max(e.ts for e in events),
        samples=events[:sample_size],
    )


def apply_reassign_default(
    session: Session,
    *,
    provider_id: str,
    source: str,
    target: str,
    event_ids: list[str] | None = None,
) -> ReassignResult:
    """Retag events from `source` to `target`, then rebuild both accounts'
    rollups (full recompute — cheap and unconditionally correct, unlike
    replaying a subtract/re-add per event) and any closed windows overlapping
    the moved events' timestamp range for either account. Commits.
    """
    plan = plan_reassign_default(
        session, provider_id=provider_id, source=source, target=target, event_ids=event_ids
    )
    if plan.count == 0:
        return ReassignResult()

    stmt = (
        update(UsageEvent)
        .where(col(UsageEvent.provider_id) == provider_id, col(UsageEvent.account_id) == source)
        .values(account_id=target, attribution_source="tag")
    )
    if event_ids:
        stmt = stmt.where(UsageEvent.event_id.in_(event_ids))  # type: ignore[attr-defined]
    session.exec(stmt)  # type: ignore[call-overload]
    session.commit()

    rebuild_rollups_for_pairs(session, {(provider_id, source), (provider_id, target)})
    session.commit()

    windows_rebuilt = rebuild_windows_overlapping(
        session,
        provider_id=provider_id,
        account_ids=[source, target],
        ts_min=plan.ts_min,  # type: ignore[arg-type]
        ts_max=plan.ts_max,  # type: ignore[arg-type]
    )

    return ReassignResult(
        moved=plan.count,
        rollups_rebuilt_pairs=2,
        windows_rebuilt=windows_rebuilt,
    )
