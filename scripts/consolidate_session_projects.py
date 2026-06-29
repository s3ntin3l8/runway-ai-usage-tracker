#!/usr/bin/env python3
"""Consolidate the `project` label of each session to its root directory.

Claude Code (and OpenCode) record a per-message `cwd` that drifts into subfolders
(`services/api`, `apps/web`, …) and git worktrees/temp dirs (`<root>/.claude/…`)
as the agent works, even when it was launched from the project root. The basename
derivation at ingest therefore scatters one repo across many `project` labels.

This is a pure-DB pass (cwd is already populated, unlike
`backfill_project_context.py` which re-reads logs): it groups events by
`session_id`, picks the session's canonical root cwd (rule 1 `.claude/` truncation
+ rule 2 shallowest cwd, see app/services/project_label.py), and relabels every
row in the session — including NULL-cwd rows — to that root's basename. The raw
per-message `cwd` is left untouched.

Idempotency is by determinism, not a NULL guard: the canonical project is a pure
function of the session's cwd set, so a second run changes nothing. Relabeling
touches only `usage_events.project`; rollups and latest_usage are not
project-keyed, so totals are unaffected.

Run `backfill_project_context.py` FIRST (fills NULL cwd from logs), then this.
Run against the dev DB or a snapshot — never the live prod DB while the server is
writing (SQLite is single-writer):

  APP_HOST=127.0.0.1 python scripts/consolidate_session_projects.py            # per-message providers
  APP_HOST=127.0.0.1 python scripts/consolidate_session_projects.py --dry-run
  APP_HOST=127.0.0.1 python scripts/consolidate_session_projects.py --provider anthropic
  APP_HOST=127.0.0.1 python scripts/consolidate_session_projects.py --all-providers
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.core.db import engine, init_db  # noqa: E402
from app.models.db import UsageEvent  # noqa: E402
from app.services.project_label import derive_project, pick_canonical_cwd  # noqa: E402

# Per-message providers, where cwd drifts within a session and rule 2 applies.
# Other providers carry a single cwd per session, so only rule 1 ever changes
# them — covered by --all-providers.
_DEFAULT_PROVIDERS = ["anthropic", "opencode", "opencode-free"]


def consolidate(session: Session, providers: list[str] | None, dry_run: bool) -> int:
    """Relabel each session's events to its canonical root project.

    `providers=None` means every provider in the table. Returns the number of
    rows whose `project` changed.
    """
    stmt = select(UsageEvent).where(UsageEvent.session_id.is_not(None))  # type: ignore[union-attr]
    if providers is not None:
        stmt = stmt.where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
    events = session.exec(stmt).all()

    by_session: dict[str, list[UsageEvent]] = defaultdict(list)
    for ev in events:
        # session_id is non-NULL by the WHERE clause above.
        by_session[ev.session_id].append(ev)  # type: ignore[index]

    total_changed = 0
    for rows in by_session.values():
        canonical = derive_project(pick_canonical_cwd(ev.cwd for ev in rows))
        if canonical is None:
            continue  # no usable cwd anywhere in the session — leave as-is
        for ev in rows:
            if ev.project != canonical:
                total_changed += 1
                if not dry_run:
                    ev.project = canonical
                    session.add(ev)

    print(
        f"{len(by_session):,} session(s), {total_changed:,} row(s) relabeled"
        + (" (dry-run)" if dry_run else ""),
        flush=True,
    )
    if not dry_run and total_changed:
        session.commit()
    return total_changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--provider", help="limit to one runway provider id")
    group.add_argument(
        "--all-providers",
        action="store_true",
        help="process every provider, not just the per-message default set",
    )
    parser.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = parser.parse_args()

    init_db()
    if args.provider:
        providers: list[str] | None = [args.provider]
    elif args.all_providers:
        providers = None
    else:
        providers = _DEFAULT_PROVIDERS

    with Session(engine) as session:
        changed = consolidate(session, providers, args.dry_run)

    verb = "would relabel" if args.dry_run else "relabeled"
    print(f"\nDone — {verb} {changed:,} event(s).", flush=True)


if __name__ == "__main__":
    main()
