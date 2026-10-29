"""Remove the events of the first Claude cloud-runner collector.

That collector (the cloud-runner change before `docs/claude-cloud-event-contract.md`) wrote one coarse
delta event per sweep from the session list's usage block: ids `cloud:{digest}:{n}`, no project, one
model per session. The replacement rebuilds the same usage from each session's history, as message and
remainder events (ids `cloud:{digest}:m:{message id}` and `cloud:{digest}:r:{ordinal}:{model}`), so the
coarse events must go before it runs or the usage would count twice. The new collector refuses to write
while any of them exist.

The rows of the collector's two state tables go too. The tables stay (they are create-only and there is
no precedent for dropping one); nothing reads them any more.

Every write is chunked and committed per batch (`_chunked_sql`), the affected rollups are rebuilt from
the events that remain, and closed windows overlapping the removed events are rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, func
from sqlmodel import Session, col, delete, select

from app.core.date_utils import parse_iso8601_utc
from app.models.db import ClaudeCloudCollectorState, ClaudeCloudSessionUsage, UsageEvent
from app.services.maintenance._chunked_sql import chunked_delete
from app.services.maintenance._retry import _retry_on_lock
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
    state_rows: int = 0
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    pairs: list[tuple[str, str]] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


@dataclass
class CoarseResult:
    events_deleted: int = 0
    state_rows_deleted: int = 0
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
    state_rows = (
        session.exec(select(func.count()).select_from(ClaudeCloudSessionUsage)).one()
        + session.exec(select(func.count()).select_from(ClaudeCloudCollectorState)).one()
    )
    pairs: list[tuple[str, str]] = []
    samples: list[str] = []
    if events:
        pairs = [
            (str(provider), str(account))
            for provider, account in session.exec(
                select(UsageEvent.provider_id, UsageEvent.account_id).where(*where).distinct()
            )
        ]
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
        state_rows=state_rows,
        ts_min=_moment(first),
        ts_max=_moment(last),
        pairs=pairs,
        samples=samples,
    )


def _clear_collector_state(session: Session) -> int:
    """The old per-account state table has no `id`, so it is cleared in one retried statement."""
    removed = chunked_delete(session, ClaudeCloudSessionUsage, [])

    def _write() -> int:
        count = session.exec(select(func.count()).select_from(ClaudeCloudCollectorState)).one()
        session.exec(delete(ClaudeCloudCollectorState))  # type: ignore[call-overload]
        session.commit()
        return count

    return removed + _retry_on_lock(
        _write, session=session, action_desc="clearing the first cloud collector's state"
    )


def apply_cloud_coarse_events(session: Session) -> CoarseResult:
    """Remove the coarse events and the old state rows, then repair what derives from them.

    Idempotent: a second run finds nothing. Interrupted runs resume: only committed batches are gone.
    """
    plan = plan_cloud_coarse_events(session)
    result = CoarseResult()
    if plan.events:
        result.events_deleted = chunked_delete(session, UsageEvent, coarse_event_where())
        # Rollups are rebuilt for the pairs captured before the delete: a pair whose events are all
        # gone would otherwise keep its rows as drift orphans.
        rebuild_rollups_for_pairs(session, set(plan.pairs))
        result.rollup_pairs_rebuilt = len(plan.pairs)
        session.commit()
        if plan.ts_min is not None and plan.ts_max is not None:
            for provider in sorted({provider for provider, _ in plan.pairs}):
                result.windows_rebuilt += rebuild_windows_overlapping(
                    session,
                    provider_id=provider,
                    account_ids=sorted(a for p, a in plan.pairs if p == provider),
                    ts_min=plan.ts_min,
                    ts_max=plan.ts_max,
                )
    if plan.state_rows:
        result.state_rows_deleted = _clear_collector_state(session)
    return result
