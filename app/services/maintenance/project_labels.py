"""Relabel usage events whose `project` label is a path instead of a name.

The label is derived server-side from the client's working directory
(`app/services/project_label.py`). A Windows client reports
`D:\\Projects\\repo`, and a derivation that split on the *server's* separator
(`os.path`, i.e. `posixpath` on the Linux server) had nothing to split on — so
the whole path became the label. Such a label is not a name: it splits one
project across as many buckets as it has checkouts, and it leaks the operator's
directory layout into the Sessions view and the Top Projects ranking.

This module is the single implementation of the relabel. The Data Health check
(`app/services/data_health/checks/path_shaped_projects.py`) and the CLI wrapper
(`scripts/consolidate_session_projects.py`) both call it, so the in-app fixer
and the script cannot drift apart.

Two scopes, one implementation:

- `plan_path_shaped_projects` — the sessions whose stored label is *path-shaped*
  (it contains a separator, which a real basename never does). This is the
  repair the Data Health check offers.
- `plan_session_relabels` — every session, whether or not its label is
  path-shaped. This is the consolidation pass the CLI has always run: a session
  whose cwd drifts into a subfolder ends up with one label per folder.
Both relabel a session as a whole: the canonical label is
`derive_project(pick_canonical_cwd(<the session's cwds>))` — rule 1 plus rule
2 — and every row of the session takes it, including the rows whose `cwd` is
NULL. That is what makes one session one project.

The relabel is an **update**: no row is inserted, no token count changes and no
rollup moves — the rollups are keyed by provider, account and period, never by
project — so only the attribution changes.

Idempotency is by determinism, not by a NULL guard: the canonical label is a
pure function of the session's cwd set, so a second run changes nothing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, func, or_
from sqlmodel import Session, col, select

from app.models.db import UsageEvent
from app.services.maintenance._chunked_sql import chunked_update
from app.services.project_label import derive_project, pick_canonical_cwd

#: How many rows one UPDATE statement carries. SQLite's parameter limit is
#: 32766 on the versions Runway ships, but a few hundred keeps the statement
#: small enough to stay inside the apply gate's write window.
_BATCH = 500


def _is_path_shaped(project: str | None) -> bool:
    """True when a stored label is a path rather than a name.

    A basename never contains a separator — on POSIX a `/` cannot be part of a
    filename, and on Windows neither `/` nor `\\` can. So the test is exact:
    there are no heuristic labels to misjudge.
    """
    if not project:
        return False
    return "/" in project or "\\" in project


def _path_shaped_filter() -> ColumnElement[bool]:
    """The SQL predicate for a path-shaped label.

    `instr` rather than `LIKE`: SQLite's `LIKE` has no default escape
    character, and the pattern for a backslash is exactly the kind of thing
    that is easy to get subtly wrong.
    """
    return or_(
        func.instr(col(UsageEvent.project), "/") > 0,
        func.instr(col(UsageEvent.project), "\\") > 0,
    )


@dataclass
class SessionRelabel:
    """One session's relabel: the label it carries now and the one it should carry."""

    session_id: str
    canonical: str
    #: How many rows the session holds.
    rows: int = 0
    #: How many of those rows carry a path-shaped label.
    path_shaped_rows: int = 0
    #: The distinct labels the session carries now.
    labels: list[str] = field(default_factory=list)
    #: The rows the apply would change.
    row_ids: list[int] = field(default_factory=list)


@dataclass
class PathShapedProjectPlan:
    """What the repair would change, and what it would leave alone.

    `path_shaped_rows` counts the rows that carry a path-shaped label — the
    findings. `rows_to_relabel` counts the rows that would actually change,
    which is more: a session that also drifted into a subfolder carries a
    second, wrongly-scattered label that the same fix corrects.
    """

    sessions: int = 0
    path_shaped_rows: int = 0
    rows_to_relabel: int = 0
    relabels: list[SessionRelabel] = field(default_factory=list)
    #: `(old label, new label, rows)` — what each label becomes.
    label_map: list[tuple[str, str, int]] = field(default_factory=list)
    #: Sessions whose rows carry a path-shaped label but hold no usable cwd, so
    #: no label can be derived for them.
    unfixable_sessions: int = 0
    unfixable_rows: int = 0
    samples: list[str] = field(default_factory=list)


@dataclass
class PathShapedProjectResult:
    """What the repair changed."""

    relabeled: int = 0
    sessions: int = 0
    path_shaped_rows: int = 0


def _plan(
    session: Session,
    *,
    providers: list[str] | None,
    restrict_to_path_shaped: bool,
    sample_size: int,
) -> PathShapedProjectPlan:
    """The sessions to relabel, with the label each should carry."""
    stmt = select(
        UsageEvent.id,
        col(UsageEvent.session_id),
        col(UsageEvent.cwd),
        col(UsageEvent.project),
    ).where(col(UsageEvent.session_id).is_not(None))
    if providers is not None:
        stmt = stmt.where(col(UsageEvent.provider_id).in_(providers))

    if restrict_to_path_shaped:
        shaped = session.exec(stmt.where(_path_shaped_filter())).all()
        session_ids = {row[1] for row in shaped if row[1] is not None}
        if not session_ids:
            return PathShapedProjectPlan()
        rows = session.exec(stmt.where(col(UsageEvent.session_id).in_(session_ids))).all()
    else:
        rows = session.exec(stmt).all()

    by_session: dict[str, list[tuple[int, str | None, str | None]]] = defaultdict(list)
    for row in rows:
        # The columns were selected in a fixed order: id, session_id, cwd, project.
        row_id, session_id, cwd, project = row[0], row[1], row[2], row[3]
        if row_id is None or session_id is None:
            continue
        by_session[session_id].append((row_id, cwd, project))

    plan = PathShapedProjectPlan()
    label_counts: dict[tuple[str, str], int] = defaultdict(int)
    for session_id, entries in sorted(by_session.items()):
        canonical = derive_project(pick_canonical_cwd(cwd for _, cwd, _ in entries))
        path_shaped_rows = sum(1 for _, _, project in entries if _is_path_shaped(project))
        if canonical is None:
            # No usable cwd anywhere in the session: no label can be derived, so
            # the rows are reported as unfixable rather than relabelled wrongly.
            if path_shaped_rows:
                plan.unfixable_sessions += 1
                plan.unfixable_rows += path_shaped_rows
            continue
        changed = [row_id for row_id, _, project in entries if project != canonical]
        if not changed:
            continue
        labels = sorted({project for _, _, project in entries if project})
        for _, _, project in entries:
            if project != canonical:
                label_counts[(project or "(none)", canonical)] += 1
        plan.relabels.append(
            SessionRelabel(
                session_id=session_id,
                canonical=canonical,
                rows=len(entries),
                path_shaped_rows=path_shaped_rows,
                labels=labels,
                row_ids=changed,
            )
        )
        plan.path_shaped_rows += path_shaped_rows
        plan.rows_to_relabel += len(changed)

    plan.sessions = len(plan.relabels)
    plan.label_map = [
        (old, new, count)
        for (old, new), count in sorted(label_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    plan.samples = [
        f"{old} → {new} ({count} row(s))" for old, new, count in plan.label_map[:sample_size]
    ]
    return plan


def plan_path_shaped_projects(
    session: Session,
    *,
    providers: list[str] | None = None,
    sample_size: int = 20,
) -> PathShapedProjectPlan:
    """The sessions whose stored label is a path, and the label they should carry.

    Only sessions that hold at least one path-shaped label are in scope: the
    repair targets exactly what is broken and leaves every other session alone.
    """
    return _plan(
        session,
        providers=providers,
        restrict_to_path_shaped=True,
        sample_size=sample_size,
    )


def plan_session_relabels(
    session: Session,
    *,
    providers: list[str] | None = None,
    sample_size: int = 20,
) -> PathShapedProjectPlan:
    """Every session's canonical label — the consolidation scope.

    A session whose cwd drifts into a subfolder ends up with one label per
    folder even when none of them is path-shaped. This pass gives each session
    the label of its root directory.
    """
    return _plan(
        session,
        providers=providers,
        restrict_to_path_shaped=False,
        sample_size=sample_size,
    )


def apply_path_shaped_projects(
    session: Session, plan: PathShapedProjectPlan
) -> PathShapedProjectResult:
    """Write the labels the plan resolved.

    Only `usage_events.project` is written. The row's tokens, costs, timestamp
    and cwd stay as they are, so no total can move.
    """
    by_label: dict[str, list[int]] = defaultdict(list)
    for relabel in plan.relabels:
        by_label[relabel.canonical].extend(relabel.row_ids)

    relabeled = 0
    for label, row_ids in by_label.items():
        for start in range(0, len(row_ids), _BATCH):
            batch = row_ids[start : start + _BATCH]
            relabeled += chunked_update(
                session,
                UsageEvent,
                [col(UsageEvent.id).in_(batch)],
                {"project": label},
                batch_size=len(batch),
            )
    return PathShapedProjectResult(
        relabeled=relabeled,
        sessions=len(plan.relabels),
        path_shaped_rows=plan.path_shaped_rows,
    )


def apply_session_relabels(
    session: Session, plan: PathShapedProjectPlan
) -> PathShapedProjectResult:
    """Write the labels the plan resolved (the consolidation scope)."""
    return apply_path_shaped_projects(session, plan)


def label_map_detail(plan: PathShapedProjectPlan) -> dict[str, Any]:
    """The plan's label mapping as primitive detail fields.

    A Data Health `Finding.detail` carries JSON primitives only, so the mapping
    is flattened into one string per label rather than nested structures.
    """
    return {
        "labels": len(plan.label_map),
        "sessions": plan.sessions,
        "path_shaped_rows": plan.path_shaped_rows,
        "rows_to_relabel": plan.rows_to_relabel,
    }
