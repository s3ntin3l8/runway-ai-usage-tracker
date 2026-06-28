#!/usr/bin/env python3
"""One-shot cleanup for the stale OpenCode "rolling free" gauge card.

Background
----------
The OpenCode web collector used to emit a standalone Free card
(provider_id="opencode", window_type="rolling", variant="Free") from the /usage
page. That duplicated the dedicated "opencode-free" passive provider, which
already tracks free-tier usage via sidecar events — so the card surfaced as a
spurious "Rolling Free" window on the OpenCode card.

The collector no longer emits that card (see
app/services/collectors/opencode.py), but latest_usage is a derived live-gauge
table that is never auto-pruned: a row persisted before the fix stays until it
is deleted. This script removes it.

latest_usage holds only live gauges — no event/rollup/history data is lost; the
remaining OpenCode cards (Go session/weekly/monthly + the API card) are
re-populated on the next collect poll.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1,
AFTER deploying the collector fix (otherwise the next poll re-creates the row):

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_opencode_free_card.py --dry-run
  # eyeball the row, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_opencode_free_card.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, delete, select  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import LatestUsage  # noqa: E402

_PROVIDER = "opencode"
_WINDOW_TYPE = "rolling"
_VARIANT = "Free"


def _filters() -> list:
    return [
        LatestUsage.provider_id == _PROVIDER,
        LatestUsage.window_type == _WINDOW_TYPE,
        LatestUsage.variant == _VARIANT,
    ]


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted without writing."
    )
    g.add_argument("--apply", action="store_true", help="Delete the stale row(s).")
    args = p.parse_args()
    dry_run = args.dry_run
    prefix = "[DRY-RUN] " if dry_run else ""

    with Session(engine) as session:
        rows = session.exec(select(LatestUsage).where(*_filters())).all()
        verb = "Would delete" if dry_run else "Deleting"
        print(
            f"{prefix}{verb} {len(rows)} latest_usage row(s) "
            f"({_PROVIDER}/{_WINDOW_TYPE}/{_VARIANT}):",
            flush=True,
        )
        for r in rows:
            print(
                f"  id={r.id} account_id={r.account_id!r} "
                f"model_id={r.model_id!r} updated_at={r.updated_at}",
                flush=True,
            )

        if not dry_run and rows:
            session.exec(delete(LatestUsage).where(*_filters()))
            session.commit()
            print(f"Deleted {len(rows)} row(s).", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
