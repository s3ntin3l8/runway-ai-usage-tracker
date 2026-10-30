#!/usr/bin/env python3
"""Consolidate the `project` label of each session to its root directory.

Claude Code (and OpenCode) record a per-message `cwd` that drifts into subfolders
(`services/api`, `apps/web`, …) and git worktrees/temp dirs (`<root>/.claude/…`)
as the agent works, even when it was launched from the project root. The basename
derivation at ingest therefore scatters one repo across many `project` labels.

A Windows client reports its working directory with backslashes (`D:\\Projects\\repo`),
and a derivation that split on the *server's* separator had nothing to split on — so
the whole path became the label. The derivation is now host-independent (see
`app/services/project_label.py`), but the rows already collected that way keep the
path until this pass relabels them.

This is a pure-DB pass (cwd is already populated, unlike
`backfill_project_context.py` which re-reads logs): it groups events by
`session_id`, picks the session's canonical root cwd (rule 1 `.claude/` truncation
+ rule 2 shallowest cwd, see app/services/project_label.py; rule 1 cuts at
`.claude`, `.wt`, `.worktrees`, `.worktree` and `.mullion-worktrees`), and relabels every
row in the session — including NULL-cwd rows — to that root's basename, or to the
repository name the session reported (`project_ref`) when it has one. The raw
per-message `cwd` is left untouched.

The relabel itself lives in `app/services/maintenance/project_labels.py`, which the
`path_shaped_projects` and `worktree_project_labels` Data Health checks use as well — one implementation, so the
in-app fixer and this script cannot drift apart.

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
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from app.core.console import force_utf8_console  # noqa: E402
from app.core.db import engine, init_db  # noqa: E402
from app.services.maintenance.project_labels import (  # noqa: E402
    apply_session_relabels,
    plan_session_relabels,
)
from scripts.sidecar_pkg.event_extractors.opencode import _OC_CANONICAL_MAP  # noqa: E402

# Per-message providers, where cwd drifts within a session and rule 2 applies.
# Other providers carry a single cwd per session, so only rule 1 ever changes
# them — covered by --all-providers. The opencode-* sub-providers (issue #182)
# all come from the same per-message sqlite log, so they drift the same way.
# _OC_CANONICAL_MAP fold-in targets (minimax, kimi_coding, ollama, ...) are
# emitted by that same log, so their per-message cwds drift identically.
_DEFAULT_PROVIDERS = [
    "anthropic",
    "opencode",
    "opencode-free",
    "opencode-byok",
    "opencode-openrouter",
    *(provider for provider, _ in _OC_CANONICAL_MAP.values()),
]


def consolidate(session: Session, providers: list[str] | None, dry_run: bool) -> int:
    """Relabel each session's events to its canonical root project.

    `providers=None` means every provider in the table. Returns the number of
    rows whose `project` changed.
    """
    plan = plan_session_relabels(session, providers=providers)
    if dry_run:
        print(
            f"{plan.sessions:,} session(s), {plan.rows_to_relabel:,} row(s) relabeled (dry-run)",
            flush=True,
        )
        return plan.rows_to_relabel

    result = apply_session_relabels(session, plan)
    print(
        f"{result.sessions:,} session(s), {result.relabeled:,} row(s) relabeled",
        flush=True,
    )
    return result.relabeled


def main() -> None:
    force_utf8_console()
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
