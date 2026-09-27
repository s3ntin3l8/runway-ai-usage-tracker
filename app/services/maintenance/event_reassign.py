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

from sqlalchemy import ColumnElement, func
from sqlmodel import Session, col, select

from app.models.db import UsageEvent
from app.services.maintenance._chunked_sql import chunked_update
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


def _where(provider_id: str, source: str, event_ids: list[str] | None) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = [
        col(UsageEvent.provider_id) == provider_id,
        col(UsageEvent.account_id) == source,
    ]
    if event_ids:
        clauses.append(col(UsageEvent.event_id).in_(event_ids))
    return clauses


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

    Aggregates in SQL rather than hydrating every matching row — a stale
    account can carry tens of thousands of events, and this preview backs
    the Data Health UI's "before you apply" check, so it needs to stay cheap
    regardless of scope size.
    """
    where = _where(provider_id, source, event_ids)
    if event_ids:
        found = {r[0] for r in session.execute(select(UsageEvent.event_id).where(*where))}
        missing = sorted(set(event_ids) - found)
        if missing:
            raise ValueError(
                f"{len(missing)} event id(s) not found under {provider_id}/{source}: {missing}"
            )

    count, ts_min, ts_max = session.execute(
        select(func.count(), func.min(UsageEvent.ts), func.max(UsageEvent.ts)).where(*where)
    ).one()
    if not count:
        return ReassignPlan()
    samples = list(
        session.exec(
            select(UsageEvent).where(*where).order_by(col(UsageEvent.ts)).limit(sample_size)
        )
    )
    return ReassignPlan(count=count, ts_min=ts_min, ts_max=ts_max, samples=samples)


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
    the moved events' timestamp range for either account.

    The retag itself is chunked (`_chunked_sql`) — a stale account like
    minimax's `default` can carry ~19.5k events, and this must never hold
    SQLite's writer lock for one giant transaction.

    Assumes `app/services/event_identity_migration.py` has already collapsed
    any cross-account duplicate events at startup — same precondition
    `legacy_retag.py` documents — so a `source`/`target` pair sharing an
    event_id can't exist here to begin with.
    """
    plan = plan_reassign_default(
        session, provider_id=provider_id, source=source, target=target, event_ids=event_ids
    )
    if plan.count == 0:
        return ReassignResult()

    chunked_update(
        session,
        UsageEvent,
        _where(provider_id, source, event_ids),
        {"account_id": target, "attribution_source": "tag"},
    )

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
