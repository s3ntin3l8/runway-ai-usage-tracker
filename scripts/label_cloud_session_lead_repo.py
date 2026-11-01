#!/usr/bin/env python3
"""Label a multi-repository Claude cloud session's unlabelled events with its
lead repository.

The cloud-runner collector names the repositories a session touched in the
session list, but the reducer labels an event with a repository only when the
session names exactly one, or when the event ran in a directory whose path
segment equals one of the names. A session that names several repositories and
never ran in a matching directory therefore leaves most of its events with no
project at all — the repository's usage is split away from it.

The repair labels those unlabelled events with the session's *lead repository*:
the `project_ref` its labelled events name most often. The evidence is the
labels the session already carries — the order of the repositories in the
session list is alphabetical and means nothing, so it is never consulted. A
session whose leading repositories tie, or that carries no label at all, is
left alone and reported as skipped.

The relabel lives in `app/services/maintenance/cloud_session_lead_repo.py`,
which the `cloud_session_lead_repo` Data Health check uses as well — one
implementation, so the in-app fixer and this script cannot drift apart.

Idempotent: a labelled event leaves the repair's scope, so a second run changes
nothing. Only `usage_events.project` and `usage_events.project_ref` are
written; the rollups are not project-keyed, so totals are unaffected.

Reports only by default; `--apply` writes — the same convention as the other
cloud repair scripts.

Run against the dev DB or a snapshot — never the live prod DB while the server is
writing (SQLite is single-writer):

  APP_HOST=127.0.0.1 python scripts/label_cloud_session_lead_repo.py
  APP_HOST=127.0.0.1 python scripts/label_cloud_session_lead_repo.py --apply
  APP_HOST=127.0.0.1 python scripts/label_cloud_session_lead_repo.py --apply --session <digest>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from app.core.console import force_utf8_console  # noqa: E402
from app.core.db import engine, init_db  # noqa: E402
from app.services.maintenance.cloud_session_lead_repo import (  # noqa: E402
    apply_cloud_session_lead_repo,
    plan_cloud_session_lead_repos,
)


def label(session: Session, session_id: str | None, apply: bool) -> int:
    """Label the unlabelled events of the matching cloud sessions.

    `session_id=None` means every cloud session with unlabelled events.
    Returns the number of events that were (or would be) labelled.
    """
    plans = plan_cloud_session_lead_repos(session)
    if session_id is not None:
        plans = [plan for plan in plans if plan.session_id == session_id]

    skipped = sum(1 for plan in plans if not plan.fixable)
    fixable = [plan for plan in plans if plan.fixable]
    events = sum(plan.unlabelled_events for plan in fixable)

    verb = "labelled" if apply else "would label"
    print(
        f"{len(fixable):,} session(s), {events:,} event(s) {verb}; {skipped:,} session(s) skipped",
        flush=True,
    )

    if not apply:
        return events

    labelled = 0
    for plan in fixable:
        result = apply_cloud_session_lead_repo(session, plan.session_id)
        labelled += result.events_labelled
        print(
            f"  {plan.session_id}: {result.events_labelled:,} event(s) labelled with "
            f"{result.lead_repo}",
            flush=True,
        )
    return labelled


def main() -> None:
    force_utf8_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session",
        help="only this cloud session digest (the session id stored on the events)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="label the events (default: report only)"
    )
    args = parser.parse_args()

    init_db()

    with Session(engine) as session:
        changed = label(session, args.session, args.apply)

    if not args.apply:
        print("\nReport only; pass --apply to label them.", flush=True)
        return

    print(f"\nDone — labelled {changed:,} event(s).", flush=True)


if __name__ == "__main__":
    main()
