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

Four scopes, one implementation:

- `plan_path_shaped_projects` — the sessions whose stored label is *path-shaped*
  (it contains a separator, which a real basename never does). This is the
  repair the Data Health check offers.
- `plan_session_relabels` — every session, whether or not its label is
  path-shaped. This is the consolidation pass the CLI has always run: a session
  whose cwd drifts into a subfolder ends up with one label per folder.
- `plan_worktree_relabels` — only the sessions that ran in a worktree directory
  (`WORKTREE_CONTAINERS`); the repair the `worktree_project_labels` check offers.
- `plan_repo_relabels` — the rows whose label should follow the *git
  repository* the client reported. The client resolves the repository on its own
  host and sends its name; the rows collected before it did keep the folder's
  name, and this pass moves them onto the repository.

Both relabel a session as a whole: the canonical label is the repository name
the session's newest row reported (`project_ref`, the same precedence the
ingestor applies) and, for a session that reported none,
`derive_project(pick_canonical_cwd(<the session's cwds>))` — rule 1 plus rule
2 — and every row of the session takes it, including the rows whose `cwd` is
NULL. That is what makes one session one project — with one exception: a row
that carries a *different* `project_ref` of its own keeps it, because the
ingestor labelled it with that name and nothing could repair a rewrite.

The relabel is an **update**: no row is inserted, no token count changes and no
rollup moves — the rollups are keyed by provider, account and period, never by
project — so only the attribution changes.

Idempotency is by determinism, not by a NULL guard: the canonical label is a
pure function of the session's cwd set, so a second run changes nothing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, func, or_, tuple_
from sqlalchemy import select as sa_select
from sqlmodel import Session, col, select

from app.models.db import UsageEvent
from app.services.maintenance._chunked_sql import chunked_update
from app.services.project_label import WORKTREE_CONTAINERS, derive_project, pick_canonical_cwd

#: How many rows one UPDATE statement carries. SQLite's parameter limit is
#: 32766 on the versions Runway ships, but a few hundred keeps the statement
#: small enough to stay inside the apply gate's write window.
_BATCH = 500


#: The separators a path may use. A client's OS decides which one it writes,
#: not the server's — so both are checked, everywhere.
_SEPARATORS = ("/", "\\")


def _is_path_shaped(project: str | None) -> bool:
    """True when a stored label is a path rather than a name.

    A basename never contains a separator — on POSIX a `/` cannot be part of a
    filename, and on Windows neither `/` nor `\\` can. So the test is exact:
    there are no heuristic labels to misjudge.

    This and `_path_shaped_filter` are the same rule, one in Python and one in
    SQL; both are built from `_SEPARATORS`, and a test pins them together.
    """
    if not project:
        return False
    return any(separator in project for separator in _SEPARATORS)


def _path_shaped_filter() -> ColumnElement[bool]:
    """The SQL predicate for a path-shaped label — `_is_path_shaped` in SQL.

    `instr` rather than `LIKE`: SQLite's `LIKE` has no default escape
    character, and the pattern for a backslash is exactly the kind of thing
    that is easy to get subtly wrong.
    """
    return or_(*(func.instr(col(UsageEvent.project), sep) > 0 for sep in _SEPARATORS))


def _worktree_filter() -> ColumnElement[bool]:
    """The SQL predicate for a cwd that holds a worktree-container segment.

    The SQL twin of `project_label`'s segment regex: a whole segment, preceded
    by either separator, followed by either separator or the end of the path,
    case-insensitive. It only narrows the rows that are loaded; the exact label
    is still derived in Python, so a false positive here costs a row read, never
    a wrong write.
    """
    cwd = func.lower(col(UsageEvent.cwd))
    clauses: list[ColumnElement[bool]] = []
    for name in WORKTREE_CONTAINERS:
        for lead in _SEPARATORS:
            segment = f"{lead}{name.lower()}"
            for trail in _SEPARATORS:
                clauses.append(func.instr(cwd, segment + trail) > 0)
            clauses.append(func.substr(cwd, -len(segment)) == segment)
    return or_(*clauses)


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


@dataclass
class RepoRelabel:
    """One working directory and the repository name its rows should carry."""

    cwd: str
    project_ref: str
    #: The host the directory lives on. A working directory is a path on one
    #: machine, and two machines can hold the same path string.
    sidecar_id: str = ""
    #: The rows this working directory holds.
    rows: int = 0
    #: The rows whose label does not follow the repository name yet.
    stale_rows: int = 0
    #: The rows the apply would change.
    row_ids: list[int] = field(default_factory=list)


@dataclass
class RepoRelabelPlan:
    """The rows whose label should follow the repository the client reported."""

    total: int = 0
    relabels: list[RepoRelabel] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


@dataclass
class RepoRelabelResult:
    """What the repair changed."""

    relabeled: int = 0
    directories: int = 0


def _plan(
    session: Session,
    *,
    providers: list[str] | None,
    scope: Literal["all", "path_shaped", "worktree"],
    sample_size: int,
) -> PathShapedProjectPlan:
    """The sessions to relabel, with the label each should carry."""
    stmt = sa_select(
        col(UsageEvent.id),
        col(UsageEvent.session_id),
        col(UsageEvent.cwd),
        col(UsageEvent.project),
        col(UsageEvent.project_ref),
        col(UsageEvent.ts),
    ).where(col(UsageEvent.session_id).is_not(None))
    if providers is not None:
        stmt = stmt.where(col(UsageEvent.provider_id).in_(providers))

    if scope != "all":
        # A subquery, not a list of session ids: the list grows with the number
        # of affected sessions, and SQLite caps a statement at 32766 parameters —
        # on a large install the list form would raise "too many SQL
        # variables" in `detect` itself, leaving the repair unreachable exactly
        # where it is needed. The same subquery keeps the scan bounded: only the
        # sessions in scope are loaded, not the whole table.
        predicate = _path_shaped_filter() if scope == "path_shaped" else _worktree_filter()
        scoped_sessions = select(col(UsageEvent.session_id)).where(predicate).distinct()
        if providers is not None:
            scoped_sessions = scoped_sessions.where(col(UsageEvent.provider_id).in_(providers))
        rows = session.execute(stmt.where(col(UsageEvent.session_id).in_(scoped_sessions))).all()
    else:
        rows = session.execute(stmt).all()

    by_session: dict[str, list[tuple[int, str | None, str | None]]] = defaultdict(list)
    # Rows that carry a repository name of their own.
    own_refs: dict[int, str] = {}
    # The repository name a session's newest reporting row carries, with the
    # (ts, id) it was reported at.
    session_refs: dict[str, tuple[Any, int, str]] = {}
    for row in rows:
        # The columns were selected in a fixed order: id, session_id, cwd,
        # project, project_ref, ts.
        row_id, session_id, cwd, project = row[0], row[1], row[2], row[3]
        project_ref, ts = row[4], row[5]
        if row_id is None or session_id is None:
            continue
        by_session[session_id].append((row_id, cwd, project))
        if project_ref:
            own_refs[row_id] = project_ref
            stamp = (ts, row_id)
            known = session_refs.get(session_id)
            if known is None or stamp > (known[0], known[1]):
                session_refs[session_id] = (ts, row_id, project_ref)

    plan = PathShapedProjectPlan()
    label_counts: dict[tuple[str, str], int] = defaultdict(int)
    for session_id, entries in sorted(by_session.items()):
        # The ingestor labels an event with the repository name the client
        # reported (`project_ref`) before it falls back to the cwd, so the
        # session plan does the same: a session that reported a repository
        # keeps it. Deriving from the cwd alone would overwrite those labels and
        # undo `inconsistent_project_labels`, which moves rows onto them.
        reported = session_refs.get(session_id)
        canonical = (
            reported[2]
            if reported is not None
            else derive_project(pick_canonical_cwd(cwd for _, cwd, _ in entries))
        )
        path_shaped_rows = sum(1 for _, _, project in entries if _is_path_shaped(project))
        if canonical is None:
            # No usable cwd anywhere in the session: no label can be derived, so
            # the rows are reported as unfixable rather than relabelled wrongly.
            if path_shaped_rows:
                plan.unfixable_sessions += 1
                plan.unfixable_rows += path_shaped_rows
            continue
        # A row that reported a different repository of its own keeps it: the
        # ingestor labelled it with that name, and `inconsistent_project_labels`
        # leaves it alone too, so nothing could repair a rewrite.
        changed = [
            row_id
            for row_id, _, project in entries
            if project != canonical and own_refs.get(row_id, canonical) == canonical
        ]
        if not changed:
            continue
        changed_ids = set(changed)
        labels = sorted({project for _, _, project in entries if project})
        for row_id, _, project in entries:
            if row_id in changed_ids:
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
        scope="path_shaped",
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
        scope="all",
        sample_size=sample_size,
    )


def plan_worktree_relabels(
    session: Session,
    *,
    providers: list[str] | None = None,
    sample_size: int = 20,
) -> PathShapedProjectPlan:
    """The sessions that ran in a worktree directory, and the label they should carry.

    Only sessions with at least one cwd inside a worktree container
    (`WORKTREE_CONTAINERS`) are in scope. A session that merely drifted into a
    subfolder is the consolidation script's business, not this repair's.
    """
    return _plan(
        session,
        providers=providers,
        scope="worktree",
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


def plan_repo_relabels(session: Session, *, sample_size: int = 20) -> RepoRelabelPlan:
    """The rows whose label should follow the repository the client reported.

    The client resolves the git repository of a working directory on its own
    host and reports the repository's *name*. Every event carries that name in
    `project_ref`, so the mapping from a working directory to a repository is
    learned from live traffic rather than guessed.

    The rows collected before the client reported one keep the label they were
    given — the folder's name. This plan finds the rows that share a working
    directory with a row that does carry a repository name, and would move them
    onto that name.

    A working directory that never appears again with a repository name keeps
    its label: the server cannot know what repository it belonged to.

    **The key is the (host, directory) pair, not the directory alone.** A
    working directory is a path on *one* machine, and two machines can hold the
    same path string — the same developer, the same folder name, two hosts. The
    rows of one host would then be relabelled onto the other host's repository,
    and each apply would undo the other.

    Two queries, not one per directory: `usage_events.cwd` carries no index, so
    a count per directory would scan the table once per directory.
    """
    reported = session.execute(
        sa_select(
            col(UsageEvent.sidecar_id),
            col(UsageEvent.cwd),
            col(UsageEvent.project_ref),
            func.max(col(UsageEvent.ts)).label("max_ts"),
            func.count().label("rows"),
        )
        .where(col(UsageEvent.project_ref).is_not(None))
        .group_by(col(UsageEvent.sidecar_id), col(UsageEvent.cwd), col(UsageEvent.project_ref))
    ).all()
    # One repository per (host, directory): the one the client reported *most
    # recently*, so a repository that was renamed is not pinned to the older
    # name. Counting the rows instead would let the name with the longest
    # history win forever — the relabel never rewrites `project_ref`, so the
    # fresh rows could never outnumber the old ones, and every scan would
    # rewrite them back. The count is only a tie-break.
    #
    # "Most recently" is the newest *timestamp*, not the newest row: an offline
    # sidecar replaying its backlog inserts pre-rename events with fresh ids,
    # and the insert order would flip the ranking back to the old name.
    newest: dict[tuple[str, str], dict[str, tuple[float, int]]] = defaultdict(dict)
    for sidecar_id, cwd, project_ref, max_ts, count in reported:
        if not cwd or not project_ref:
            continue
        # `max(ts)` is never NULL for a group that has rows. The stored value
        # comes back naive — it is UTC (see `iso_utc`) — so it is stamped as
        # UTC before the conversion: the ordering survives either way, but the
        # comparison must not depend on the deploying host's timezone.
        stamp = max_ts.replace(tzinfo=UTC).timestamp() if isinstance(max_ts, datetime) else 0.0
        newest[(sidecar_id, cwd)][project_ref] = (stamp, count)
    refs: dict[tuple[str, str], str] = {
        key: min(
            by_ref,
            # Newest report first, then the most-reported name, then the
            # lexicographic order — so the choice is the same on every run.
            key=lambda ref: (-by_ref[ref][0], -by_ref[ref][1], ref),
        )
        for key, by_ref in newest.items()
    }
    if not refs:
        return RepoRelabelPlan()

    rows = session.execute(
        sa_select(
            col(UsageEvent.id),
            col(UsageEvent.sidecar_id),
            col(UsageEvent.cwd),
            col(UsageEvent.project),
            col(UsageEvent.project_ref),
        ).where(tuple_(col(UsageEvent.sidecar_id), col(UsageEvent.cwd)).in_(list(refs)))
    ).all()

    totals: dict[tuple[str, str], int] = defaultdict(int)
    stale: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in rows:
        row_id, sidecar_id, cwd, project, project_ref = row[0], row[1], row[2], row[3], row[4]
        if row_id is None or not cwd:
            continue
        key = (sidecar_id, cwd)
        if key not in refs:
            continue
        totals[key] += 1
        # A row that carries a repository name of its own is not a target: it
        # reported a repository, and the repair is for the rows that were
        # labelled with the *folder's* name before the client reported one.
        if project_ref is not None:
            continue
        if project != refs[key]:
            stale[key].append(row_id)

    plan = RepoRelabelPlan()
    for key in sorted(refs):
        row_ids = stale.get(key)
        if not row_ids:
            continue
        project_ref = refs[key]
        plan.relabels.append(
            RepoRelabel(
                cwd=key[1],
                sidecar_id=key[0],
                project_ref=project_ref,
                rows=totals[key],
                stale_rows=len(row_ids),
                row_ids=row_ids,
            )
        )
        plan.total += len(row_ids)
        if len(plan.samples) < sample_size:
            plan.samples.append(f"{key[0]}:{key[1]} → {project_ref} ({len(row_ids)} row(s))")
    return plan


def apply_repo_relabels(session: Session, plan: RepoRelabelPlan) -> RepoRelabelResult:
    """Move the rows the plan found onto the repository name.

    Only `usage_events.project` is written: the raw working directory and the
    repository the client reported stay as they are, so the move is reversible
    and no total can move.

    The rows are written by id, in batches: `usage_events.cwd` carries no
    index, so a per-directory UPDATE would scan the table once per directory.
    """
    by_ref: dict[str, list[int]] = defaultdict(list)
    for relabel in plan.relabels:
        by_ref[relabel.project_ref].extend(relabel.row_ids)

    relabeled = 0
    for project_ref, row_ids in by_ref.items():
        for start in range(0, len(row_ids), _BATCH):
            batch = row_ids[start : start + _BATCH]
            relabeled += chunked_update(
                session,
                UsageEvent,
                [col(UsageEvent.id).in_(batch)],
                {"project": project_ref},
                batch_size=len(batch),
            )
    return RepoRelabelResult(relabeled=relabeled, directories=len(plan.relabels))
