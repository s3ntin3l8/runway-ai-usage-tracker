"""Label a multi-repository Claude cloud session's unlabelled events with its
lead repository.

The cloud-runner collector names the repositories a session touched in the
session list (`config.outcomes`), and the reducer
(`app/services/claude_cloud_events.py::_project`) labels an event with a
repository name only when the session names exactly one, or when the event's
working directory holds a path segment equal to one of the names. A session
that names *several* repositories and never ran in a directory that matches one
of them therefore leaves most of its events with no `project` and no
`project_ref` at all — they show up as a project of their own (or none) in the
project filter, and the session's usage is split away from the repository it
belongs to.

**The rule is evidence based, never a guess.** The only evidence about which
repository a session belongs to is the labels its own events already carry: the
session's *lead repository* is the `project_ref` that the most of its labelled
events name. The order of the repositories in the session list is alphabetical
and carries no meaning, so it is never consulted; neither is the branch, the
tool paths in the history, or the event's own working directory. When two
repositories tie for the most events, or when the session has no labelled event
at all, the session is left alone — a wrong project is worse than none.

Only the unlabelled events of the session (`project IS NULL AND
project_ref IS NULL`) are touched, and they get the same repository in both
columns. An event that already carries a label keeps it, whatever repository it
names.

**Only the history-based collector's events are in scope** — the
`cloud:<digest>:m:...` and `cloud:<digest>:r:...` ids, through
`cloud_events_reset.cloud_event_where()`. The first collector's coarse ids
(`cloud:<digest>:<n>`) are not: those rows carry no project and no per-model
detail, and `cloud_coarse_events` removes them.

**No derived table is rebuilt.** `project` is a column of `usage_events` only —
the rollups are keyed by provider, account and period, and the windows by
provider and time — so relabelling events cannot leave a cached total behind.
The write is an update: no row is inserted, no token count changes and no
total moves.

The repair lives here, and the Data Health check
(`app/services/data_health/checks/cloud_session_lead_repo.py`) and the CLI
wrapper (`scripts/label_cloud_session_lead_repo.py`) both call it, so the
in-app fixer and the script cannot drift apart.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlmodel import Session, col

from app.models.db import UsageEvent
from app.services.maintenance._chunked_sql import chunked_update
from app.services.maintenance.cloud_events_reset import cloud_event_where


@dataclass
class CloudSessionLeadRepoPlan:
    """What the repair would do to one cloud session."""

    session_id: str
    #: The repository the session's labelled events name most often. None when
    #: the session is left alone.
    lead_repo: str | None = None
    #: How many of the session's events carry a label.
    labelled_events: int = 0
    #: How many of the session's events carry no label at all.
    unlabelled_events: int = 0
    #: How many different repositories the session's labelled events name.
    candidate_repos: int = 0
    #: Why the session is left alone: "tie" (two repositories share the lead)
    #: or "no_labelled_events" (the session names no repository at all).
    skipped_reason: str | None = None

    @property
    def fixable(self) -> bool:
        return self.lead_repo is not None and self.unlabelled_events > 0


@dataclass
class CloudSessionLeadRepoResult:
    """What the repair actually did to one cloud session."""

    session_id: str
    lead_repo: str | None = None
    events_labelled: int = 0
    skipped_reason: str | None = None


def _plans(session: Session, session_id: str | None = None) -> list[CloudSessionLeadRepoPlan]:
    """The plan for every cloud session with unlabelled events.

    Two grouped aggregates, not a per-event scan: the labelled events are
    counted per repository, and the unlabelled ones per session. A session with
    no unlabelled event has nothing to repair and never appears.
    """
    cloud = cloud_event_where()
    labelled_stmt = (
        sa_select(
            col(UsageEvent.session_id),
            col(UsageEvent.project_ref),
            func.count().label("rows"),
        )
        .where(*cloud, col(UsageEvent.project_ref).is_not(None))
        .group_by(col(UsageEvent.session_id), col(UsageEvent.project_ref))
    )
    unlabelled_stmt = (
        sa_select(
            col(UsageEvent.session_id),
            func.count().label("rows"),
        )
        .where(
            *cloud,
            col(UsageEvent.project).is_(None),
            col(UsageEvent.project_ref).is_(None),
        )
        .group_by(col(UsageEvent.session_id))
    )
    if session_id is not None:
        labelled_stmt = labelled_stmt.where(col(UsageEvent.session_id) == session_id)
        unlabelled_stmt = unlabelled_stmt.where(col(UsageEvent.session_id) == session_id)

    repos_by_session: dict[str, dict[str, int]] = defaultdict(dict)
    for row_session_id, project_ref, rows in session.execute(labelled_stmt).all():
        if row_session_id and project_ref:
            repos_by_session[row_session_id][project_ref] = rows

    plans: list[CloudSessionLeadRepoPlan] = []
    for row_session_id, rows in session.execute(unlabelled_stmt).all():
        if not row_session_id:
            continue
        repos = repos_by_session.get(row_session_id, {})
        plan = CloudSessionLeadRepoPlan(
            session_id=row_session_id,
            labelled_events=sum(repos.values()),
            unlabelled_events=rows,
            candidate_repos=len(repos),
        )
        if not repos:
            # Nothing to learn from: the session names no repository at all.
            plan.skipped_reason = "no_labelled_events"
        else:
            best = max(repos.values())
            winners = sorted(repo for repo, count in repos.items() if count == best)
            if len(winners) > 1:
                # Two repositories share the lead — never guess between them.
                plan.skipped_reason = "tie"
            else:
                plan.lead_repo = winners[0]
        plans.append(plan)
    return plans


def plan_cloud_session_lead_repos(session: Session) -> list[CloudSessionLeadRepoPlan]:
    """Read-only preview for every cloud session with unlabelled events."""
    return _plans(session)


def plan_cloud_session_lead_repo(session: Session, session_id: str) -> CloudSessionLeadRepoPlan:
    """Read-only preview for one cloud session."""
    plans = _plans(session, session_id)
    if plans:
        return plans[0]
    # The session has no unlabelled event (any more): nothing to repair.
    return CloudSessionLeadRepoPlan(session_id=session_id)


def apply_cloud_session_lead_repo(session: Session, session_id: str) -> CloudSessionLeadRepoResult:
    """Label the session's unlabelled events with its lead repository.

    The write is chunked and retried on a locked database (see
    `app/services/maintenance/_chunked_sql.py`), and it is idempotent: a
    labelled event no longer matches the scope, so a second run changes
    nothing.
    """
    plan = plan_cloud_session_lead_repo(session, session_id)
    if not plan.fixable:
        return CloudSessionLeadRepoResult(
            session_id=session_id,
            lead_repo=plan.lead_repo,
            events_labelled=0,
            skipped_reason=plan.skipped_reason,
        )

    events_labelled = chunked_update(
        session,
        UsageEvent,
        [
            *cloud_event_where(),
            col(UsageEvent.session_id) == session_id,
            col(UsageEvent.project).is_(None),
            col(UsageEvent.project_ref).is_(None),
        ],
        {"project": plan.lead_repo, "project_ref": plan.lead_repo},
    )
    return CloudSessionLeadRepoResult(
        session_id=session_id,
        lead_repo=plan.lead_repo,
        events_labelled=events_labelled,
    )
