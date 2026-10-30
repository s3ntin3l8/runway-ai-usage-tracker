"""Reset the history-based Claude cloud-runner events so the collector rebuilds them.

Cloud events are deduplicated on ``(provider_id, event_id)``, so a change to how they are labelled (the
sidecar id they are ingested under, the project they are attributed to) never reaches rows that are
already stored. The reset removes both halves of what the collector keeps: the events (ids
``cloud:{digest}:m:{message id}`` and ``cloud:{digest}:r:{ordinal}:{model}``) and the per-session
checkpoints. Events alone would leave nothing to re-emit (the checkpoints say the history was read);
checkpoints alone would replay into the old rows and keep their old labels. With both gone the next
collector cycle reads every session's history from the start.

The first collector's coarse events (``cloud_coarse_events``) are a different set and are not touched, so
the migration guard of the collector is unaffected.

Every write is chunked and committed per batch. The rollups are rebuilt from the events that remain,
*whether or not this run deleted anything*, so a run that died between the delete and the repair is
finished by applying again. Closed windows are rebuilt for the accounts and time range of the events this
run removed (a window replay drops any window that cannot be rebuilt from events, so it must not touch
windows the removal did not reach); ``windows_range`` replays that step of an interrupted run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, func, or_
from sqlmodel import Session, col, select

from app.models.db import ClaudeCloudSessionCheckpoint, UsageEvent, UsageWindow
from app.services import claude_cloud_collector
from app.services.maintenance._chunked_sql import chunked_delete
from app.services.maintenance.cloud_coarse_events import (
    PROVIDER_ID,
    derived_pairs,
    moment,
    window_where,
)
from app.services.maintenance.rollups import rebuild_rollups_for_pairs
from app.services.maintenance.windows import rebuild_windows_overlapping

ENTRYPOINT = "cloud"


def cloud_event_where() -> list[ColumnElement[bool]]:
    """Events of the history-based collector: ``cloud:<digest>:m:...`` and ``cloud:<digest>:r:...``."""
    return [
        col(UsageEvent.provider_id) == PROVIDER_ID,
        col(UsageEvent.entrypoint) == ENTRYPOINT,
        or_(
            col(UsageEvent.event_id).like("cloud:%:m:%"),
            col(UsageEvent.event_id).like("cloud:%:r:%"),
        ),
    ]


@dataclass
class ResetPlan:
    events: int = 0
    sessions: int = 0
    checkpoints: int = 0
    windows: int = 0
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    accounts: list[str] = field(default_factory=list)
    sidecar_ids: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


@dataclass
class ResetResult:
    events_deleted: int = 0
    checkpoints_deleted: int = 0
    rollup_pairs_rebuilt: int = 0
    windows_rebuilt: int = 0
    ts_min: datetime | None = None
    ts_max: datetime | None = None


def plan_cloud_events_reset(session: Session, *, sample_size: int = 5) -> ResetPlan:
    """What would be removed. Read-only."""
    where = cloud_event_where()
    events, sessions = session.exec(
        select(func.count(), func.count(func.distinct(UsageEvent.session_id))).where(*where)
    ).one()
    checkpoints = session.exec(select(func.count()).select_from(ClaudeCloudSessionCheckpoint)).one()
    plan = ResetPlan(events=events, sessions=sessions, checkpoints=checkpoints)
    if not events:
        return plan
    first, last = session.exec(
        select(func.min(UsageEvent.ts), func.max(UsageEvent.ts)).where(*where)
    ).one()
    plan.ts_min, plan.ts_max = moment(first), moment(last)
    plan.accounts = sorted(
        session.exec(select(UsageEvent.account_id).where(*where).distinct()).all()
    )
    plan.sidecar_ids = sorted(
        str(value) for value in session.exec(select(UsageEvent.sidecar_id).where(*where).distinct())
    )
    if plan.ts_min is not None and plan.ts_max is not None:
        plan.windows = session.exec(
            select(func.count())
            .select_from(UsageWindow)
            .where(*window_where(plan.accounts, plan.ts_min, plan.ts_max))
        ).one()
    plan.samples = list(
        session.exec(
            select(UsageEvent.event_id)
            .where(*where)
            .order_by(col(UsageEvent.event_id))
            .limit(sample_size)
        )
    )
    return plan


def apply_cloud_events_reset(
    session: Session, *, windows_range: tuple[datetime, datetime] | None = None
) -> ResetResult:
    """Remove the cloud events and checkpoints, then repair what derives from the events."""
    plan = plan_cloud_events_reset(session)
    result = ResetResult(ts_min=plan.ts_min, ts_max=plan.ts_max)
    if plan.events:
        result.events_deleted = chunked_delete(session, UsageEvent, cloud_event_where())
    if plan.checkpoints:
        result.checkpoints_deleted = chunked_delete(session, ClaudeCloudSessionCheckpoint, [])
    # The collector's in-memory backlog names sessions by the checkpoints just removed.
    claude_cloud_collector.clear_pending()
    pairs = derived_pairs(session)
    if pairs:
        rebuild_rollups_for_pairs(session, pairs)
        result.rollup_pairs_rebuilt = len(pairs)
        session.commit()
    if plan.events and plan.ts_min is not None and plan.ts_max is not None:
        window_start, window_end, accounts = plan.ts_min, plan.ts_max, plan.accounts
    elif windows_range is not None:
        window_start, window_end = windows_range
        accounts = sorted({account for _, account in pairs})
    else:
        return result
    if accounts:
        result.windows_rebuilt = rebuild_windows_overlapping(
            session,
            provider_id=PROVIDER_ID,
            account_ids=accounts,
            ts_min=window_start,
            ts_max=window_end,
        )
    return result
