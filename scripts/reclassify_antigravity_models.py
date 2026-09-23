#!/usr/bin/env python3
"""Repair antigravity usage_events written before minor-version preservation.

Background
----------
scripts/sidecar_pkg/event_extractors/antigravity.py's _normalize_ag_model()
used to collapse every Gemini family to a coarse 3.x-or-not bucket — "3.5
Flash" / "3.6 Flash" / "3.7 Flash" / "3.8 Flash" all became "flash-3", and
"3.1 Pro" became "pro-3" — so distinct Google rates billed at the shared
flash-3/pro-3 seed row. It also never read the display-name effort suffix
("(High)"/"(Medium)"/"(Low)") at all. The extractor now preserves the minor
version (when it starts with 3.x) and captures effort; this script re-reads
the on-disk conversation DBs with the corrected extractor and repairs rows
already ingested under the old lossy mapping.

Matching is by event_id (`<conversation_uuid>|gen_<idx>`, stable across
replays as long as the source DB is only appended to — see
parse_antigravity_events). Rows whose source DB has since been pruned are
left untouched and counted separately; there is no other source for their
original model_id.

After repairing model_id/effort, run scripts/recost_events.py --provider
antigravity to reprice the affected rows from provider_pricing (the newly
seeded flash-3.5..3.8 / pro-3.1 / bare-family rows). Start the server once
first so seed_pricing_table() inserts those rows on an existing DB.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/reclassify_antigravity_models.py --dry-run
  # eyeball the planned changes, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/reclassify_antigravity_models.py
  python scripts/recost_events.py --provider antigravity
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.core.db import engine, init_db  # noqa: E402
from app.models.db import UsageEvent  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from scripts.backfill_rollups import backfill as rebuild_rollups  # noqa: E402
from scripts.sidecar import _discover_antigravity_db_paths  # noqa: E402
from scripts.sidecar_pkg.event_extractors.antigravity import (  # noqa: E402
    parse_antigravity_events,
)

# Reach back far enough to cover all retained history.
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def _collect_pushes() -> dict[str, UsageEventPush]:
    """Re-parse on-disk conversation DBs -> {event_id: push} under the fixed extractor."""
    paths = _discover_antigravity_db_paths()
    if not paths:
        # Fallback for environments where the sidecar helper is unavailable
        # or the conversations dir moved — same glob the helper uses.
        base = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
        paths = sorted(base.glob("*.db")) if base.is_dir() else []
    pushes = parse_antigravity_events(paths, "backfill", _EPOCH)
    return {p.event_id: p for p in pushes}


def reclassify(session: Session, dry_run: bool) -> int:
    """Repair model_id/effort on existing antigravity usage_events. Returns rows changed."""
    pushes = _collect_pushes()
    if not pushes:
        print("No Antigravity conversation DBs found; nothing to repair.", flush=True)
        return 0

    rows = session.exec(select(UsageEvent).where(UsageEvent.provider_id == "antigravity")).all()
    changed = 0
    missing = 0
    for row in rows:
        # event_id is "<conversation_uuid>|gen_<idx>" — this match is only
        # correct under the append-only invariant real conversation DBs hold.
        # A DB recreated in place at the same path with the same stem but
        # different content at a given idx would silently attribute that
        # row's (model_id, effort) to the wrong original event, with no
        # detection here (raw_json is NULL on these rows). The printed
        # per-row diff plus --dry-run is the only safety net.
        push = pushes.get(row.event_id)
        if push is None:
            missing += 1
            continue
        new_model_id = push.model_id
        new_effort = push.effort
        if new_model_id == row.model_id and new_effort == row.effort:
            continue
        prefix = "[DRY-RUN] " if dry_run else ""
        print(
            f"{prefix}{row.event_id}: model_id {row.model_id!r} -> {new_model_id!r}, "
            f"effort {row.effort!r} -> {new_effort!r}",
            flush=True,
        )
        if not dry_run:
            row.model_id = new_model_id
            row.effort = new_effort
            session.add(row)
        changed += 1

    if not dry_run and changed:
        session.commit()

    print(
        f"\n{'[DRY-RUN] ' if dry_run else ''}{changed:,} row(s) "
        f"{'would be' if dry_run else ''} repaired "
        f"({missing:,} not found in on-disk conversation DBs).",
        flush=True,
    )
    return changed


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = p.parse_args()

    init_db()
    with Session(engine) as session:
        events_changed = reclassify(session, args.dry_run)

    if not args.dry_run and events_changed:
        print("Rebuilding rollups for: antigravity", flush=True)
        rebuild_rollups(["antigravity"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
