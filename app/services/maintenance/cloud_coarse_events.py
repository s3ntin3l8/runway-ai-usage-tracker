"""Remove the events of the first Claude cloud-runner collector.

That collector (the cloud-runner change before `docs/claude-cloud-event-contract.md`) wrote one coarse
delta event per sweep from the session list's usage block: ids `cloud:{digest}:{n}`, no project, one
model per session. The replacement rebuilds the same usage from each session's history, as message and
remainder events (ids `cloud:{digest}:m:{message id}` and `cloud:{digest}:r:{ordinal}:{model}`), so the
coarse events must go before it runs or the usage would count twice. The new collector refuses to write
while any of them exist.

The collector's two state tables are deliberately left alone. They are create-only, nothing reads them
once the replacement is in place, and clearing them while the first collector is still scheduled would
reset its delta baseline and bill every session again.

Every write is chunked and committed per batch (`_chunked_sql`). The rollups are then rebuilt from the
events that remain, *whether or not this run deleted anything*, so a run that died between the delete and
the repair is finished by applying again. Closed windows are rebuilt only for the accounts and time
range of the events this run removed: a window replay drops any window that cannot be rebuilt from
events, so it must not touch windows the removal did not reach. That range is known only to the run
that deleted (a rerun after an interruption repairs rollups but not windows).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, func
from sqlmodel import Session, col, select

from app.core.date_utils import parse_iso8601_utc
from app.models.db import UsageEvent, UsagePeriodRollup, UsageWindow
from app.services.maintenance._chunked_sql import chunked_delete
from app.services.maintenance.rollups import rebuild_rollups_for_pairs
from app.services.maintenance.windows import rebuild_windows_overlapping

PROVIDER_ID = "anthropic"
ENTRYPOINT = "cloud"


def coarse_event_where() -> list[ColumnElement[bool]]:
    """Events of the first collector: `cloud:` ids that are not of the new `:m:` / `:r:` forms."""
    return [
        col(UsageEvent.provider_id) == PROVIDER_ID,
        col(UsageEvent.entrypoint) == ENTRYPOINT,
        col(UsageEvent.event_id).like("cloud:%"),
        col(UsageEvent.event_id).not_like("cloud:%:m:%"),
        col(UsageEvent.event_id).not_like("cloud:%:r:%"),
    ]


@dataclass
class CoarsePlan:
    events: int = 0
    sessions: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_create: int = 0
    windows: int = 0
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    accounts: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


@dataclass
class CoarseResult:
    events_deleted: int = 0
    rollup_pairs_rebuilt: int = 0
    windows_rebuilt: int = 0


def _moment(value: object) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else parse_iso8601_utc(str(value))


def plan_cloud_coarse_events(session: Session, *, sample_size: int = 5) -> CoarsePlan:
    """What would be removed. Read-only."""
    where = coarse_event_where()
    events, sessions = session.exec(
        select(func.count(), func.count(func.distinct(UsageEvent.session_id))).where(*where)
    ).one()
    t_in, t_out, t_read, t_create = session.exec(
        select(
            func.coalesce(func.sum(UsageEvent.tokens_input), 0),
            func.coalesce(func.sum(UsageEvent.tokens_output), 0),
            func.coalesce(func.sum(UsageEvent.tokens_cache_read), 0),
            func.coalesce(func.sum(UsageEvent.tokens_cache_create), 0),
        ).where(*where)
    ).one()
    first, last = session.exec(
        select(func.min(UsageEvent.ts), func.max(UsageEvent.ts)).where(*where)
    ).one()
    samples: list[str] = []
    accounts: list[str] = []
    windows = 0
    first_ts, last_ts = _moment(first), _moment(last)
    if events:
        accounts = sorted(
            session.exec(select(UsageEvent.account_id).where(*where).distinct()).all()
        )
        if first_ts is not None and last_ts is not None:
            windows = session.exec(
                select(func.count())
                .select_from(UsageWindow)
                .where(*_window_where(accounts, first_ts, last_ts))
            ).one()
        samples = list(
            session.exec(
                select(UsageEvent.event_id)
                .where(*where)
                .order_by(col(UsageEvent.event_id))
                .limit(sample_size)
            )
        )
    return CoarsePlan(
        events=events,
        sessions=sessions,
        tokens_input=int(t_in),
        tokens_output=int(t_out),
        tokens_cache_read=int(t_read),
        tokens_cache_create=int(t_create),
        windows=windows,
        ts_min=first_ts,
        ts_max=last_ts,
        accounts=accounts,
        samples=samples,
    )


def _window_where(
    accounts: list[str], ts_min: datetime, ts_max: datetime
) -> list[ColumnElement[bool]]:
    return [
        col(UsageWindow.provider_id) == PROVIDER_ID,
        col(UsageWindow.account_id).in_(accounts),
        col(UsageWindow.window_start) <= ts_max,
        col(UsageWindow.window_end) > ts_min,
    ]


def _derived_pairs(session: Session) -> set[tuple[str, str]]:
    """Every Anthropic (provider, account) with events, rollup rows or closed windows."""
    pairs: set[tuple[str, str]] = set()
    for model in (UsageEvent, UsagePeriodRollup, UsageWindow):
        pairs.update(
            (str(provider), str(account))
            for provider, account in session.exec(
                select(model.provider_id, model.account_id)  # type: ignore[attr-defined]
                .where(col(model.provider_id) == PROVIDER_ID)  # type: ignore[attr-defined]
                .distinct()
            )
        )
    return pairs


def apply_cloud_coarse_events(session: Session) -> CoarseResult:
    """Remove the coarse events, then repair what derives from them.

    Rollups are repaired on every call (an interrupted run is finished by applying again); closed
    windows only for the range this run removed (see the module docstring).
    """
    plan = plan_cloud_coarse_events(session)
    result = CoarseResult()
    if plan.events:
        result.events_deleted = chunked_delete(session, UsageEvent, coarse_event_where())
    # Pairs come from every derived table too, so one whose events are all gone still has its
    # rollup rows rebuilt (to nothing) instead of lingering as drift orphans.
    pairs = _derived_pairs(session)
    if pairs:
        rebuild_rollups_for_pairs(session, pairs)
        result.rollup_pairs_rebuilt = len(pairs)
        session.commit()
    if plan.events and plan.ts_min is not None and plan.ts_max is not None:
        result.windows_rebuilt = rebuild_windows_overlapping(
            session,
            provider_id=PROVIDER_ID,
            account_ids=plan.accounts,
            ts_min=plan.ts_min,
            ts_max=plan.ts_max,
        )
    return result
