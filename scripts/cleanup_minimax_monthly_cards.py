#!/usr/bin/env python3
"""One-shot cleanup for the stale MiniMax "monthly" gauge cards.

Background
----------
The MiniMax collector used to parse a `remains` field that never existed in
MiniMax's real /v1/coding_plan/remains response, and stamped every card with
DEFAULT_WINDOW_TYPE="monthly" — a window MiniMax's Coding Plan doesn't even
have (it's a 5h rolling window + a weekly window). Those cards always carried
limit_value=0.0 and no reset_at (see app/services/collectors/minimax.py).

The collector now emits window_type="session"/"weekly" cards with a real
pct_used and reset_at instead, but latest_usage is a derived live-gauge table
that is never auto-pruned: rows written under the old shape stay until deleted
— prune_stale_latest_usage() only fires on a sidecar push, and skips rows
whose card_json has no reset_at (exactly the "monthly" rows this collector
used to write), so it can't clean these up on its own.

This script removes them. latest_usage holds only live gauges — no
event/rollup/history data is lost; the correct session/weekly MiniMax cards
are re-populated on the next collect poll.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1,
AFTER deploying the collector fix (otherwise the next poll re-creates rows
under the old shape):

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_minimax_monthly_cards.py --dry-run
  # eyeball the row(s), then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_minimax_monthly_cards.py --apply
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

_PROVIDER = "minimax"
_WINDOW_TYPE = "monthly"


def _filters() -> list:
    return [
        LatestUsage.provider_id == _PROVIDER,
        LatestUsage.window_type == _WINDOW_TYPE,
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
            f"{prefix}{verb} {len(rows)} latest_usage row(s) ({_PROVIDER}/{_WINDOW_TYPE}):",
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
