#!/usr/bin/env python3
"""One-shot cleanup for the stale Anthropic "Sonnet Weekly" gauge card.

Background
----------
Anthropic restructured Claude's plan quotas: the model-specific Sonnet weekly
window (API key ``seven_day_sonnet``) is gone — only the aggregate ``weekly``
(model_id=None) and the 5-hour ``session`` windows remain for all models. The
API stopped emitting ``seven_day_sonnet`` around 2026-06-30.

The collector no longer produces the Sonnet Weekly card (see
app/services/collectors/_anthropic_common.py and anthropic_oauth.py's core_keys),
but latest_usage is a derived live-gauge table that is never auto-pruned: the row
persisted from the last poll before the fix stays until it is deleted. That row
is provider_id="anthropic", window_type="weekly", model_id="sonnet". This script
removes it.

latest_usage holds only live gauges — no event/rollup/history data is lost. The
closed-window archive in usage_windows is intentionally left untouched (that
Sonnet weekly history is legitimate past data, not stale gauge state). The
remaining Anthropic cards (session + aggregate weekly) are re-populated on the
next collect poll.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1,
AFTER deploying the collector fix (otherwise the next poll re-creates the row):

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_anthropic_sonnet_weekly.py --dry-run
  # eyeball the row, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_anthropic_sonnet_weekly.py --apply
  # repeat against the dev DB:
  RUNWAY_CONFIG_DIR=./data APP_HOST=127.0.0.1 \\
      python scripts/cleanup_anthropic_sonnet_weekly.py --apply
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

_PROVIDER = "anthropic"
_WINDOW_TYPE = "weekly"
_MODEL_ID = "sonnet"


def _filters() -> list:
    return [
        LatestUsage.provider_id == _PROVIDER,
        LatestUsage.window_type == _WINDOW_TYPE,
        LatestUsage.model_id == _MODEL_ID,
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
            f"({_PROVIDER}/{_WINDOW_TYPE}/{_MODEL_ID}):",
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
