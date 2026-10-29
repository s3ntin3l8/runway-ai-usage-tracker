#!/usr/bin/env python3
"""Remove the events of the first Claude cloud-runner collector.

That collector wrote one coarse event per sweep (ids `cloud:{digest}:{n}`, no project, one model per
session). Its replacement rebuilds the same usage from each session's history and does not run while
the coarse events exist, so remove them first. The Anthropic rollups are then rebuilt from what remains (on
every run, so an interrupted run is finished by running it again) and so are the closed windows that
overlapped the removed events (only in the run that removed them). The collector's two state tables are left alone.

The repair itself lives in `app/services/maintenance/cloud_coarse_events.py`, which the
`cloud_coarse_events` Data Health check uses as well, so the in-app fixer and this script cannot drift.

Run with the server stopped (SQLite is single-writer), or use the Data Health page instead:

  APP_HOST=127.0.0.1 python scripts/remove_cloud_coarse_events.py            # report only
  APP_HOST=127.0.0.1 python scripts/remove_cloud_coarse_events.py --apply

The run prints the time range of the events it removed. If it was interrupted after the delete, the
rollups are repaired by running it again; to replay the window rebuild too, pass that range:

  APP_HOST=127.0.0.1 python scripts/remove_cloud_coarse_events.py --apply \
      --windows-from 2026-09-24T00:00:00+00:00 --windows-to 2026-10-28T23:59:59+00:00
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
from app.core.date_utils import parse_iso8601_utc  # noqa: E402
from app.core.db import engine, init_db  # noqa: E402
from app.services.maintenance.cloud_coarse_events import (  # noqa: E402
    apply_cloud_coarse_events,
    plan_cloud_coarse_events,
)


def main() -> None:
    force_utf8_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="remove the rows (default: report only)"
    )
    args = parser.parse_args()

    init_db()
    with Session(engine) as session:
        plan = plan_cloud_coarse_events(session)
        print(
            f"{plan.events:,} coarse event(s) across {plan.sessions:,} session(s)",
            flush=True,
        )
        if not args.apply:
            print("\nReport only; pass --apply to remove them.", flush=True)
            return
        windows_range = None
        if args.windows_from and args.windows_to:
            windows_range = (
                parse_iso8601_utc(args.windows_from),
                parse_iso8601_utc(args.windows_to),
            )
        result = apply_cloud_coarse_events(session, windows_range=windows_range)
    if result.ts_min and result.ts_max:
        print(
            f"\nRemoved events spanned {result.ts_min.isoformat()} to {result.ts_max.isoformat()}.",
            flush=True,
        )
    print(
        f"\nDone: removed {result.events_deleted:,} event(s); rebuilt "
        f"{result.rollup_pairs_rebuilt:,} rollup pair(s) and {result.windows_rebuilt:,} window(s). "
        "Run it again if it was interrupted: the rollups are repaired every time.",
        flush=True,
    )


if __name__ == "__main__":
    main()
