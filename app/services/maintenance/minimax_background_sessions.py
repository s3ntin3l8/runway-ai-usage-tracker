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

A plain re-push of the same row deduplicates on its event id, so the repair
stands. A re-push under a *different* account is a retag: the server
refreshes the stored row from the sidecar's payload, so a sidecar that has
not been upgraded yet puts the row back on the child. Re-running the repair
heals it.

A row that is still parked in ``pending_usage_events`` (an identity-less push
waiting for its account) is outside the repair: the promotion writes the
payload the sidecar sent, so the repair has to run again after the operator
assigns the account.

The mapping comes from the MiniMax Code runtime store, which lives on the
host that ran MiniMax Code. The server never reads it (see ``AGENTS.md``), so
this module takes the mapping as an argument and the ``scripts/`` wrapper
(``scripts/link_minimax_background_sessions.py``) is what reads the store.

There is a second, server-side path: a row that already carries its provenance
names the session it belongs to, so the server can file it itself. That is
what ``plan_provenance_relinks`` / ``apply_provenance_relinks`` do, and what
the ``minimax_background_sessions`` Data Health check offers. It only sees the
rows that carry a provenance — the ones collected before the extractor learned
the parent link carry none, and the store is the only place that can map
those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import case, func
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
    """One row's attribution: where it belongs and what it is.

    ``parent_session_id`` is ``None`` when the parent session is not in the
    runtime store: the row then keeps its own session id and is only tagged,
    which is what the extractor does with the same row.
    """

    event_id: str
    parent_session_id: str | None
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

    A link without a parent session is only tagged: the row keeps its own
    session id, which is what the extractor does with the same row.

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
        filed_under_parent = (
            link.parent_session_id is None or row.session_id == link.parent_session_id
        )
        if (
            filed_under_parent
            and row.subagent_type == link.subagent_type
            and (link.turn_id is None or row.turn_id == link.turn_id)
        ):
            plan.already_linked += 1
            continue
        link.row_id = row.id
        plan.linked.append(link)
        if len(plan.samples) < sample_size:
            plan.samples.append(f"{link.event_id} -> {link.parent_session_id or 'its own session'}")
    return plan


def apply_background_attribution(
    session: Session, plan: BackgroundLinkPlan
) -> BackgroundLinkResult:
    """Move the rows the plan would move.

    Only the attribution is written: the session the row belongs to, the tag
    that marks it as subagent work, the turn it belongs to and the provenance
    that names the session the work really came from. The row's tokens, costs
    and timestamp are never touched, so no total can move.

    A link without a parent session keeps the row's own session id and is
    only tagged.
    """
    linked = 0
    for link in plan.linked:
        if link.row_id is None:
            continue
        row = session.get(UsageEvent, link.row_id)
        if row is None:
            continue
        if link.parent_session_id is not None:
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


@dataclass
class ProvenanceRelink:
    """One row whose provenance names a parent it is not filed under."""

    event_id: str
    row_id: int
    session_id: str
    parent_session_id: str


@dataclass
class ProvenanceRelinkPlan:
    """The rows the server-side repair would move."""

    total: int = 0
    relinks: list[ProvenanceRelink] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


@dataclass
class ProvenanceRelinkResult:
    """What the server-side repair moved."""

    relinked: int = 0


def _provenance_parent(column: Any) -> Any:
    """The parent session a row's provenance names, or None.

    Two guards, both needed:

    - ``json_extract`` raises on malformed JSON, so the extraction sits behind
      a ``json_valid`` test in a CASE — SQLite evaluates the THEN branch only
      when the condition holds.
    - The value is only taken when it is **text**. ``raw_json`` is an
      arbitrary string the ingest schema accepts, so a payload may name a
      numeric or a nested parent; writing one of those into ``session_id``
      would file the row under a session id that cannot exist.
    """
    return case(
        (
            func.json_valid(column) == 1,
            case(
                (
                    func.json_type(column, "$.parent_session_id") == "text",
                    # Trimmed here, not in Python: the comparison and the
                    # group key must see the same value, or a padded id would
                    # be reported as fixable and never move.
                    func.trim(func.json_extract(column, "$.parent_session_id")),
                ),
                else_=None,
            ),
        ),
        else_=None,
    )


def _relink_stmt(
    provider_id: str,
    source_app: str | None,
    child_session_id: str | None,
    parent_session_id: str | None,
) -> Any:
    """The rows whose own provenance names a parent they are not filed under."""
    parent = _provenance_parent(UsageEvent.raw_json)
    stmt = select(
        UsageEvent.id,
        UsageEvent.event_id,
        UsageEvent.session_id,
        parent.label("parent_session_id"),
    ).where(
        UsageEvent.provider_id == provider_id,
        col(UsageEvent.raw_json).is_not(None),
        parent.is_not(None),
        parent != UsageEvent.session_id,
    )
    if source_app is not None:
        # The same rule the store-based repair uses: a row collected before the
        # `source_app` column existed carries the provider id there, which is
        # the backfill's guess and the ingestor's own fallback.
        stmt = stmt.where(col(UsageEvent.source_app).in_([source_app, provider_id]))
    if child_session_id is not None:
        stmt = stmt.where(UsageEvent.session_id == child_session_id)
    if parent_session_id is not None:
        stmt = stmt.where(parent == parent_session_id)
    return stmt


def plan_provenance_relinks(
    session: Session,
    *,
    child_session_id: str | None = None,
    parent_session_id: str | None = None,
    provider_id: str = PROVIDER_ID,
    source_app: str | None = SOURCE_APP,
) -> ProvenanceRelinkPlan:
    """The rows whose own provenance names a parent they are not filed under.

    A MiniMax Code background-task row carries the provenance of the session
    it really belongs to. When such a row sits on the background task's own
    session instead, the work shows up in the Sessions view as a conversation
    of its own — and the session that spawned it does not show it as subagent
    work.

    The provenance is the only thing consulted: the server never reads the
    runtime store, so it can only see the rows that already carry one.
    """
    stmt = _relink_stmt(provider_id, source_app, child_session_id, parent_session_id)
    plan = ProvenanceRelinkPlan()
    for row in session.exec(stmt).all():
        parent = row.parent_session_id
        if not isinstance(parent, str):
            continue
        # A session id is not blank and does not carry whitespace inside it.
        # The provenance is sidecar-supplied text, and a blank value would
        # file the row under a session id that cannot exist. (The outer
        # padding is trimmed by the query.)
        if not parent or any(char.isspace() for char in parent):
            continue
        plan.relinks.append(
            ProvenanceRelink(
                event_id=row.event_id,
                row_id=row.id,
                session_id=row.session_id,
                parent_session_id=parent,
            )
        )
    plan.total = len(plan.relinks)
    plan.samples = [f"{r.event_id} -> {r.parent_session_id}" for r in plan.relinks[:5]]
    return plan


def apply_provenance_relinks(
    session: Session, plan: ProvenanceRelinkPlan
) -> ProvenanceRelinkResult:
    """Move the rows the plan would move under the parent they name.

    Only the session changes. The row's tokens, costs, timestamp, subagent
    tag and turn id stay as they are: the provenance already says which
    session the work belongs to, and the tag it already carries is the one
    the extractor gave it.
    """
    relinked = 0
    for relink in plan.relinks:
        row = session.get(UsageEvent, relink.row_id)
        if row is None:
            continue
        row.session_id = relink.parent_session_id
        session.add(row)
        relinked += 1
    session.commit()
    return ProvenanceRelinkResult(relinked=relinked)
