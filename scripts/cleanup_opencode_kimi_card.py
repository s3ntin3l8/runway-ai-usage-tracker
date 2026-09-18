#!/usr/bin/env python3
"""One-shot cleanup for the stale OpenCode-derived Kimi Coding gauge card.

Background
----------
OpenCode messages served by its `kimi-code-plan-global` backend used to land on
the derived provider_id "opencode-kimi-code-plan-global" (see
map_opencode_provider_id in scripts/sidecar_pkg/event_extractors/opencode.py).
_OC_CANONICAL_MAP now retags them onto the canonical "kimi_coding" provider so
they enrich the Kimi Coding card, and scripts/reclassify_opencode_providers.py
migrates the already-ingested usage_events rows.

latest_usage is a derived live-gauge table that is never auto-pruned: the old
"opencode-kimi-code-plan-global" gauge row stays until deleted. This script
removes it. Only live gauges are touched — no event/rollup/history data is
lost; the Kimi Coding cards are re-populated by the kimi_coding collector on
the next poll.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1,
AFTER deploying the collector + extractor changes AND running
reclassify_opencode_providers.py --providers opencode-kimi-code-plan-global:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_opencode_kimi_card.py --dry-run
  # eyeball the row(s), then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_opencode_kimi_card.py --apply
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

_PROVIDER = "opencode-kimi-code-plan-global"


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
        rows = session.exec(select(LatestUsage).where(LatestUsage.provider_id == _PROVIDER)).all()
        verb = "Would delete" if dry_run else "Deleting"
        print(f"{prefix}{verb} {len(rows)} latest_usage row(s) ({_PROVIDER}):", flush=True)
        for r in rows:
            print(
                f"  id={r.id} account_id={r.account_id!r} window_type={r.window_type!r} "
                f"model_id={r.model_id!r} updated_at={r.updated_at}",
                flush=True,
            )

        if not dry_run and rows:
            session.exec(delete(LatestUsage).where(LatestUsage.provider_id == _PROVIDER))
            session.commit()
            print(f"Deleted {len(rows)} row(s).", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
