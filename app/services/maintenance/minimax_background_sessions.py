"""File the MiniMax Code background-task rows under the session that spawned them.

The MiniMax Code extractor files a background task's rows under the **parent**
session and tags them ``subagent_type="Explore"`` — the shape a Claude Code
sidechain row has (a sidechain message carries the parent's session id plus
an attribution agent). Rows collected **before** that change carry the
background task's own session id and no tag, so they show up in the Sessions
view as a conversation of their own.

This repair moves them onto the parent. It is an **update**: no row is
inserted, no token count changes and no rollup moves — the rollups are keyed
by provider, account and period, never by session — so only the attribution
changes. A project's *session count* does drop: a background task was never a
conversation.

The mapping comes from the MiniMax Code runtime store, which lives on the
host that ran MiniMax Code. The server never reads it (see ``AGENTS.md``), so
this module takes the mapping as an argument and the ``scripts/`` wrapper
(``scripts/link_minimax_background_sessions.py``) is what reads the store.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, col, select

from app.models.db import UsageEvent

#: The provider the MiniMax Code runtime bills against.
PROVIDER_ID = "minimax"

#: The client app the rows come from. The repair only touches these, so a
#: row of the same provider that came from somewhere else is left alone.
SOURCE_APP = "minimax_code"

#: How many event ids are looked up per query.
_LOOKUP_BATCH = 200


@dataclass
class BackgroundLink:
    """One row's attribution: where it belongs and what it is."""

    event_id: str
    parent_session_id: str
    subagent_type: str
    turn_id: str | None = None
    raw_json: str | None = None
    #: The row this link was resolved to. Filled in by the plan.
    row_id: int | None = None


@dataclass
class BackgroundLinkPlan:
    """What the repair would change, and what it would leave alone."""

    total: int = 0
    linked: list[BackgroundLink] = field(default_factory=list)
    already_linked: int = 0
    missing: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


@dataclass
class BackgroundLinkResult:
    """What the repair changed."""

    linked: int = 0
    already_linked: int = 0
    missing: int = 0


def _rows_by_event_id(
    session: Session, event_ids: list[str], provider_id: str, source_app: str | None
) -> dict[str, UsageEvent]:
    """Return the stored rows for these event ids, keyed by event id.

    ``(provider_id, event_id)`` is unique, so the lookup is unambiguous. The
    rows are fetched in batches so a large repair does not build one giant
    ``IN`` clause.
    """
    found: dict[str, UsageEvent] = {}
    for start in range(0, len(event_ids), _LOOKUP_BATCH):
        chunk = event_ids[start : start + _LOOKUP_BATCH]
        stmt = select(UsageEvent).where(
            UsageEvent.provider_id == provider_id,
            col(UsageEvent.event_id).in_(chunk),
        )
        if source_app is not None:
            # A row collected before the `source_app` column existed carries
            # the provider id there — that is the backfill's guess and the
            # ingestor's own fallback — so both count as this client app.
            stmt = stmt.where(col(UsageEvent.source_app).in_([source_app, provider_id]))
        for row in session.exec(stmt).all():
            found[row.event_id] = row
    return found


def plan_background_attribution(
    session: Session,
    links: list[BackgroundLink],
    *,
    provider_id: str = PROVIDER_ID,
    source_app: str | None = SOURCE_APP,
    sample_size: int = 5,
) -> BackgroundLinkPlan:
    """What the repair would move, and what it would leave alone.

    A link whose row is already filed under the parent and already tagged is
    counted as done, so a second run is a no-op. A link whose row is not in
    the table at all is reported as missing — it was never collected, or the
    store was reset since. Neither is invented.

    The links are annotated in place with the row they resolved to, so the
    apply step does not have to look them up again.
    """
    plan = BackgroundLinkPlan(total=len(links))
    if not links:
        return plan

    rows = _rows_by_event_id(session, [link.event_id for link in links], provider_id, source_app)
    for link in links:
        row = rows.get(link.event_id)
        if row is None:
            plan.missing.append(link.event_id)
            continue
        if (
            row.session_id == link.parent_session_id
            and row.subagent_type == link.subagent_type
            and (link.turn_id is None or row.turn_id == link.turn_id)
        ):
            plan.already_linked += 1
            continue
        link.row_id = row.id
        plan.linked.append(link)
        if len(plan.samples) < sample_size:
            plan.samples.append(f"{link.event_id} -> {link.parent_session_id}")
    return plan


def apply_background_attribution(
    session: Session, plan: BackgroundLinkPlan
) -> BackgroundLinkResult:
    """Move the rows the plan would move.

    Only the attribution is written: the session the row belongs to, the tag
    that marks it as subagent work, the turn it belongs to and the provenance
    that names the session the work really came from. The row's tokens, costs
    and timestamp are never touched, so no total can move.
    """
    linked = 0
    for link in plan.linked:
        if link.row_id is None:
            continue
        row = session.get(UsageEvent, link.row_id)
        if row is None:
            continue
        row.session_id = link.parent_session_id
        row.subagent_type = link.subagent_type
        if link.turn_id is not None:
            row.turn_id = link.turn_id
        if link.raw_json is not None:
            row.raw_json = link.raw_json
        session.add(row)
        linked += 1
    session.commit()
    return BackgroundLinkResult(
        linked=linked,
        already_linked=plan.already_linked,
        missing=len(plan.missing),
    )
