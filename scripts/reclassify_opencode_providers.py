#!/usr/bin/env python3
"""One-shot reclassification of OpenCode events mislabeled before issue #182's fix.

Background
----------
scripts/sidecar_pkg/event_extractors/opencode.py used to fold every OpenCode
providerID except "opencode" (free tier) into "opencode" (the Go/paid tier) —
so bring-your-own-key, OpenRouter, and Ollama Cloud traffic silently counted
as Go subscription usage, and failed requests (bad auth, no subscription)
counted as real messages. See map_opencode_provider_id() /
_classify_opencode_error() in that module for the corrected logic the
extractor now applies going forward — this script reapplies the same logic to
events already ingested under the old buggy mapping.

For each usage_events row currently tagged provider_id in ("opencode",
"opencode-free"), this script re-reads the local OpenCode SQLite database
(msg_id == event_id, a stable join), recomputes the correct provider_id (and
kind="error" for failed requests, zeroing their token/cost fields to match
what a fresh error-event ingest produces), and UPDATEs the row in place.
Afterward it rebuilds usage_period_rollup for every affected provider via
scripts/backfill_rollups.py — events are the source of truth; latest_usage /
fleet cards are computed live from usage_events and need no separate update.

Must run on the host where opencode.db lives (local topology) — the sidecar's
raw per-message data isn't retained server-side. Rows whose event_id is no
longer present in opencode.db (pruned locally, or ingested from a different
host) are left untouched and reported.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/reclassify_opencode_providers.py --dry-run
  # eyeball the planned reclassification, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/reclassify_opencode_providers.py --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Ensure repo root is on sys.path when the script is invoked directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import engine  # noqa: E402
from app.models.db import UsageEvent  # noqa: E402
from scripts.backfill_rollups import backfill  # noqa: E402
from scripts.sidecar_pkg.event_extractors.opencode import (  # noqa: E402
    _classify_opencode_error,
    map_opencode_provider_id,
)

# The only two provider_ids the old (buggy) extractor ever wrote to.
_OLD_PROVIDERS = ("opencode", "opencode-free")

# {msg_id: (correct_provider_id, kind, error_reason)}
_ReclassifyMap = dict[str, tuple[str, str, str | None]]


def _read_opencode_db(db_path: Path) -> _ReclassifyMap:
    """Recompute the correct provider_id/kind for every assistant message."""
    if not db_path.exists():
        return {}

    mapping: _ReclassifyMap = {}
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, data FROM message WHERE json_extract(data, '$.role') = 'assistant'")
        for msg_id, data_json in cur.fetchall():
            try:
                data = json.loads(data_json) if data_json else {}
            except Exception:
                continue
            provider_id = map_opencode_provider_id(data.get("providerID") or "")
            err = data.get("error")
            if isinstance(err, dict):
                mapping[msg_id] = (provider_id, "error", _classify_opencode_error(err))
            else:
                mapping[msg_id] = (provider_id, "message", None)
    finally:
        conn.close()
    return mapping


def migrate(db_path: Path, apply: bool) -> int:
    """Reclassify usage_events rows. Returns the number of rows changed."""
    mapping = _read_opencode_db(db_path)
    if not mapping:
        print(f"No OpenCode assistant messages found in {db_path}; nothing to do.", flush=True)
        return 0

    prefix = "" if apply else "[DRY-RUN] "
    changed = 0
    skipped_missing = 0
    skipped_conflict = 0
    touched_providers: set[str] = set()

    with Session(engine) as session:
        rows = session.exec(
            select(UsageEvent).where(UsageEvent.provider_id.in_(_OLD_PROVIDERS))  # type: ignore[attr-defined]
        ).all()
        for row in rows:
            target = mapping.get(row.event_id)
            if target is None:
                skipped_missing += 1
                continue  # not in the local DB anymore (pruned, other host, ...)

            new_provider_id, new_kind, error_reason = target
            if new_provider_id == row.provider_id and new_kind == row.kind:
                continue  # already correct

            old_provider_id, old_kind = row.provider_id, row.kind
            row.provider_id = new_provider_id
            row.kind = new_kind
            if new_kind == "error":
                row.stop_reason = error_reason
                row.tokens_input = 0
                row.tokens_output = 0
                row.tokens_cache_read = 0
                row.tokens_cache_create = 0
                row.tokens_reasoning = 0
                row.cost_usd = 0.0
                row.cost_input = 0.0
                row.cost_output = 0.0
                row.cost_cache_read = 0.0
                row.cost_cache_create = 0.0

            if apply:
                # Guard the (provider_id, account_id, event_id) unique constraint
                # per-row so an unexpected collision doesn't abort the whole batch.
                sp = session.begin_nested()
                try:
                    session.add(row)
                    session.flush()
                except IntegrityError:
                    sp.rollback()
                    row.provider_id, row.kind = old_provider_id, old_kind
                    skipped_conflict += 1
                    print(
                        f"  ! {row.event_id}: skipped, {new_provider_id}/{row.account_id} "
                        "already exists",
                        flush=True,
                    )
                    continue
                sp.commit()

            changed += 1
            touched_providers.add(old_provider_id)
            touched_providers.add(new_provider_id)
            print(
                f"{prefix}{row.event_id}: {old_provider_id}/{old_kind} "
                f"-> {new_provider_id}/{new_kind}",
                flush=True,
            )

        if apply and changed:
            session.commit()

    print(
        f"\n{prefix}{changed:,} row(s) {'reclassified' if apply else 'would be reclassified'} "
        f"({skipped_missing:,} not found locally, {skipped_conflict:,} conflicts skipped).",
        flush=True,
    )

    if apply and touched_providers:
        print(f"Rebuilding rollups for: {', '.join(sorted(touched_providers))}", flush=True)
        backfill(sorted(touched_providers))

    return changed


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to opencode.db (default: settings.OPENCODE_DB_PATH).",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing."
    )
    g.add_argument("--apply", action="store_true", help="Reclassify rows and rebuild rollups.")
    args = p.parse_args()

    db_path = args.db or Path(settings.OPENCODE_DB_PATH)
    migrate(db_path, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
