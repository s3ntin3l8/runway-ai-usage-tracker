#!/usr/bin/env python3
"""Re-price old events that were billed through the family price fallback.

Thin wrapper over app/services/maintenance/fallback_priced_events.py, shared
with the Data Health `fallback_priced_events` fix. Stop the server first so
SQLite has one writer. Prints counts and dollars only.

  python scripts/reprice_fallback_priced_events.py --provider anthropic --model sonnet-5 --dry-run
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
from app.core.db import engine  # noqa: E402
from app.services.maintenance.fallback_priced_events import (  # noqa: E402
    apply_fallback_priced_events,
    plan_fallback_priced_events,
)


def run(provider: str, model: str, dry_run: bool) -> int:
    with Session(engine) as session:
        if dry_run:
            plan = plan_fallback_priced_events(session, provider, model)
            print(
                f"[DRY-RUN] {plan.events} event(s): {plan.stored_usd:.2f} -> "
                f"{plan.new_usd:.2f} USD ({plan.delta_usd:+.2f})"
            )
        else:
            result = apply_fallback_priced_events(session, provider, model)
            print(
                f"Re-priced {result.events} event(s): {result.stored_usd:.2f} -> "
                f"{result.new_usd:.2f} USD ({result.delta_usd:+.2f}); "
                f"{result.rollups_rebuilt_pairs} rollup pair(s), "
                f"{result.windows_rebuilt} window(s) rebuilt"
            )
    return 0


def main() -> int:
    force_utf8_console()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return run(args.provider, args.model, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
