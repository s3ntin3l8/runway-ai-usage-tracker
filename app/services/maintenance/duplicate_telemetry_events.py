"""Remove Claude Code telemetry events that duplicate a transcript event.

The sidecar captures Claude Code's OpenTelemetry ``api_request`` events (``otel|<request_id>``)
for the calls its transcripts never record (titles, summaries, prompt suggestions, ...). It
skips the calls the transcripts already hold by their ``query_source``. Claude Desktop's local
Code sessions drive Claude Code through the SDK, and their main loop reports
``query_source = "sdk"``, which the receiver did not treat as covered until the fix that
ships with this repair. Each such call was stored twice: once from the transcript and once
from telemetry, under the same ``request_id``.

A telemetry event is a duplicate when the same provider, account and session hold a
non-telemetry event with its ``request_id``. The telemetry copy is the one removed: the
transcript event carries the project, branch and cost detail. The rollups of the affected
accounts are rebuilt from the events that remain, as are the closed windows overlapping the
removed events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, exists, func
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.date_utils import parse_iso8601_utc
from app.models.db import UsageEvent
from app.services.maintenance.chunked_sql import chunked_delete
from app.services.maintenance.rollups import rebuild_rollups_for_pairs
from app.services.maintenance.windows import rebuild_windows_overlapping

PROVIDER_ID = "anthropic"
# The sidecar receiver's event-id prefix (``scripts/sidecar_pkg/otel_receiver.EVENT_ID_PREFIX``).
TELEMETRY_PREFIX = "otel|"


def duplicate_where() -> list[ColumnElement[bool]]:
    """Telemetry events whose request id is already stored as a transcript event.

    The id prefix is a range on the ``(provider_id, event_id)`` unique index and the match goes
    through the ``(provider_id, account_id, session_id, ts)`` index, so neither side scans the
    whole table.
    """
    other = aliased(UsageEvent)
    return [
        col(UsageEvent.provider_id) == PROVIDER_ID,
        col(UsageEvent.event_id) >= TELEMETRY_PREFIX,
        # "}" sorts directly after "|": the half-open range is exactly the prefix.
        col(UsageEvent.event_id) < TELEMETRY_PREFIX[:-1] + "}",
        col(UsageEvent.request_id).is_not(None),
        exists().where(
            col(other.provider_id) == col(UsageEvent.provider_id),
            col(other.account_id) == col(UsageEvent.account_id),
            col(other.session_id) == col(UsageEvent.session_id),
            col(other.request_id) == col(UsageEvent.request_id),
            col(other.event_id).not_like(TELEMETRY_PREFIX + "%"),
        ),
    ]


@dataclass
class DuplicatePlan:
    events: int = 0
    sessions: int = 0
    cost_usd: float = 0.0
    tokens_output: int = 0
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    accounts: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


@dataclass
class DuplicateResult:
    events_deleted: int = 0
    rollup_pairs_rebuilt: int = 0
    windows_rebuilt: int = 0


def _moment(value: object) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else parse_iso8601_utc(str(value))


def plan_duplicate_telemetry_events(session: Session, *, sample_size: int = 5) -> DuplicatePlan:
    """What would be removed. Read-only."""
    where = duplicate_where()
    events, sessions, cost, output = session.exec(
        select(
            func.count(),
            func.count(func.distinct(UsageEvent.session_id)),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
            func.coalesce(func.sum(UsageEvent.tokens_output), 0),
        ).where(*where)
    ).one()
    first, last = session.exec(
        select(func.min(UsageEvent.ts), func.max(UsageEvent.ts)).where(*where)
    ).one()
    plan = DuplicatePlan(
        events=int(events),
        sessions=int(sessions),
        cost_usd=float(cost),
        tokens_output=int(output),
        ts_min=_moment(first),
        ts_max=_moment(last),
    )
    if plan.events:
        plan.accounts = sorted(
            session.exec(select(UsageEvent.account_id).where(*where).distinct()).all()
        )
        plan.samples = list(
            session.exec(
                select(UsageEvent.event_id)
                .where(*where)
                .order_by(col(UsageEvent.event_id))
                .limit(sample_size)
            )
        )
    return plan


def apply_duplicate_telemetry_events(session: Session, *, batch_size: int = 500) -> DuplicateResult:
    """Delete the duplicates, then rebuild what derives from them.

    The rows are read first and the read transaction ended before any write (the WAL
    escalation ``chunked_sql`` documents); they are then deleted ``batch_size`` ids per
    write transaction. The accounts and the time range rebuilt are those of the rows actually
    removed, so a duplicate committed after the read is left for the next run, rollups and
    windows included, instead of being deleted without its dependents rebuilt.
    """
    result = DuplicateResult()
    rows = list(
        session.exec(
            select(UsageEvent.id, UsageEvent.account_id, UsageEvent.ts)
            .where(*duplicate_where())
            .order_by(col(UsageEvent.id))
        )
    )
    session.commit()  # end the read transaction before writing
    if not rows:
        return result
    ids = [int(row_id) for row_id, _, _ in rows if row_id is not None]
    for start in range(0, len(ids), batch_size):
        result.events_deleted += chunked_delete(
            session,
            UsageEvent,
            [col(UsageEvent.id).in_(ids[start : start + batch_size])],
            batch_size=batch_size,
        )

    accounts = sorted({str(account) for _, account, _ in rows})
    stamps = [moment for _, _, ts in rows if (moment := _moment(ts)) is not None]
    rebuild_rollups_for_pairs(session, {(PROVIDER_ID, account) for account in accounts})
    session.commit()
    result.rollup_pairs_rebuilt = len(accounts)
    if stamps:
        result.windows_rebuilt = rebuild_windows_overlapping(
            session,
            provider_id=PROVIDER_ID,
            account_ids=accounts,
            ts_min=min(stamps),
            ts_max=max(stamps),
        )
    return result
