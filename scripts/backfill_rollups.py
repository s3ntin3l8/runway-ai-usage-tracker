#!/usr/bin/env python3
"""Rebuild usage_period_rollup from existing usage_events.

Use this after retagging events (e.g. opencode → opencode-free) or whenever
the rollup table has drifted from reality. Deletes the affected rollup rows
first so the rebuild is clean — events are the source of truth.

Examples:
  # Rebuild for one provider
  python scripts/backfill_rollups.py --provider opencode-free

  # Rebuild for several providers
  python scripts/backfill_rollups.py --provider opencode --provider opencode-free

  # Rebuild everything (no filter)
  python scripts/backfill_rollups.py --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root is on sys.path when the script is invoked directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, delete, select  # noqa: E402

from app.core.console import force_utf8_console  # noqa: E402
from app.core.db import engine  # noqa: E402
from app.models.db import UsageEvent, UsagePeriodRollup  # noqa: E402
from app.services.period_rollups import update_rollups_for_event  # noqa: E402


def backfill(providers: list[str] | None) -> int:
    """Rebuild rollups for the given providers (or all if None). Returns events processed."""
    with Session(engine) as session:
        if providers:
            session.exec(
                delete(UsagePeriodRollup).where(UsagePeriodRollup.provider_id.in_(providers))  # type: ignore[attr-defined]
            )
            stmt = (
                select(UsageEvent)
                .where(UsageEvent.kind == "message")
                .where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
                .order_by(UsageEvent.ts)
            )
        else:
            session.exec(delete(UsagePeriodRollup))
            stmt = select(UsageEvent).where(UsageEvent.kind == "message").order_by(UsageEvent.ts)

        session.commit()

        events = session.exec(stmt).all()
        print(f"Processing {len(events):,} event(s)…", flush=True)
        for i, ev in enumerate(events, start=1):
            update_rollups_for_event(session, ev)
            if i % 1000 == 0:
                session.commit()
                print(f"  …{i:,}", flush=True)
        session.commit()
        return len(events)


def main() -> int:
    force_utf8_console()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--provider",
        action="append",
        default=[],
        help="Provider id to rebuild (repeatable). Omit with --all for everything.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Rebuild rollups for every provider in usage_events.",
    )
    args = p.parse_args()

    if not args.provider and not args.all:
        p.error("supply --provider <id> (repeatable) or --all")

    providers = args.provider if not args.all else None
    n = backfill(providers)
    scope = "all providers" if providers is None else ", ".join(providers)
    print(f"Done. Rebuilt rollups for {scope} from {n:,} event(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
