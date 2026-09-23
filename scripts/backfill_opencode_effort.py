#!/usr/bin/env python3
"""Backfill OpenCode message `variant` onto already-ingested usage_events.effort.

The OpenCode extractor (scripts/sidecar_pkg/event_extractors/opencode.py) used
to discard the per-message `variant` field (`high` / `medium` / absent), so
historical `usage_events` rows have `effort` at NULL. OpenCode's session-level
`session.model.variant` is not a per-turn source — only the assistant message's
own `variant` field is authoritative.

This script re-parses the local OpenCode SQLite DB(s) with the fixed extractor,
matches by `event_id` across every opencode-derived provider id (the
`opencode` prefix plus the canonical retags `minimax` / `kimi_coding` from
`_OC_CANONICAL_MAP`), and updates `effort` only — cost, tokens, and rollup
aggregates are untouched. Effort does not feed rollup dimensions; Phase C
still rebuilds `usage_period_rollup` for touched providers as belt-and-braces.

Must run on the host where opencode.db lives (local topology) — the sidecar's
raw per-message data isn't retained server-side. Rows whose event_id is no
longer present in opencode.db (pruned locally, or ingested from a different
host) are left untouched.

Run with the server STOPPED (SQLite is single-writer) and point
RUNWAY_CONFIG_DIR at the target DB:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/backfill_opencode_effort.py --dry-run
  # eyeball the planned updates, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/backfill_opencode_effort.py

Verify after apply:

  # effort populated on opencode-derived message rows
  sqlite3 "$RUNWAY_CONFIG_DIR/runway.db" \\
    "SELECT provider_id, effort, COUNT(*) FROM usage_events \\
     WHERE kind='message' AND (provider_id LIKE 'opencode%' \\
       OR provider_id IN ('minimax','kimi_coding')) \\
     GROUP BY provider_id, effort;"

  # or via the dashboard: Insights → per-model effort, if surfaced
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
from scripts.backfill_rollups import backfill as backfill_rollups  # noqa: E402
from scripts.sidecar import _opencode_account_email  # noqa: E402
from scripts.sidecar_pkg.event_extractors.opencode import (  # noqa: E402
    _OC_CANONICAL_MAP,
    parse_opencode_events,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Same prefix convention as scripts/backfill_project_context.py: OpenCode
# splits into several runway provider_ids at ingest (Go, free, byok, ...).
_OPENCODE_PROVIDER_PREFIX = "opencode"
# Canonical retags from _OC_CANONICAL_MAP (minimax, kimi_coding, ...).
_CANONICAL_TARGETS = tuple(pid for pid, _ in _OC_CANONICAL_MAP.values())


def _discover_db_paths(db_override: Path | None = None) -> list[Path]:
    """Return existing OpenCode DB paths (both historical locations), or [override]."""
    if db_override is not None:
        if db_override.exists():
            return [db_override]
        raise FileNotFoundError(
            f"--db override not found: {db_override} "
            "(default discovery looks at ~/.local/share/opencode and ~/.opencode)"
        )
    candidates = [
        Path.home() / ".local/share/opencode/opencode.db",
        Path.home() / ".opencode/opencode.db",
    ]
    return [p for p in candidates if p.exists()]


def _collect_pushes(db_paths: list[Path]) -> dict[str, UsageEventPush]:
    """Re-parse OpenCode DB(s) → {event_id: push}. First path wins on collision."""
    if not db_paths:
        return {}
    pushes: dict[str, UsageEventPush] = {}
    for db_path in db_paths:
        # Resolve identity per path — two locations can carry different accounts.
        account_id = _opencode_account_email(db_path)
        for push in parse_opencode_events(db_path, account_id=account_id, since=_EPOCH):
            if push.event_id not in pushes:
                pushes[push.event_id] = push
    return pushes


def _pick_target(candidates: list[UsageEvent], push: UsageEventPush) -> UsageEvent:
    """Prefer provider_id match, then account_id; SQL tie-break is (ts, provider_id, account_id)."""
    by_provider = [c for c in candidates if c.provider_id == push.provider_id]
    pool = by_provider or candidates
    by_account = [c for c in pool if c.account_id == push.account_id]
    if by_account:
        return by_account[0]
    return pool[0]


def phase_b_effort(
    session: Session, pushes: dict[str, UsageEventPush], dry_run: bool
) -> tuple[int, set[str]]:
    """Update effort on opencode-derived message rows; return (changed, touched_providers)."""
    stmt = (
        select(UsageEvent)
        .where(
            UsageEvent.kind == "message",
            (
                UsageEvent.provider_id.startswith(_OPENCODE_PROVIDER_PREFIX)  # type: ignore[attr-defined]
                | UsageEvent.provider_id.in_(list(_CANONICAL_TARGETS))  # type: ignore[attr-defined]
            ),
        )
        .order_by(UsageEvent.ts, UsageEvent.provider_id, UsageEvent.account_id)
    )
    events = session.exec(stmt).all()
    print(f"Phase B — examining {len(events):,} opencode-derived message(s)…", flush=True)

    # Index candidates by event_id for multi-provider / multi-account matching.
    by_event_id: dict[str, list[UsageEvent]] = {}
    for ev in events:
        by_event_id.setdefault(ev.event_id, []).append(ev)

    changed = 0
    matched = 0
    touched: set[str] = set()
    for event_id, push in pushes.items():
        candidates = by_event_id.get(event_id)
        if not candidates:
            continue
        matched += 1
        # Prefer provider_id, then account_id (same event_id can exist under
        # multiple providers, e.g. opencode + kimi_coding retags).
        target = _pick_target(candidates, push)
        if target.effort == push.effort:
            continue
        # Fill-only: a push with no variant must not clear a non-NULL effort.
        if push.effort is None and target.effort is not None:
            continue
        changed += 1
        touched.add(target.provider_id)
        if not dry_run:
            target.effort = push.effort
            session.add(target)
        if changed % 1000 == 0:
            if not dry_run:
                session.commit()
            print(f"  …{changed:,}", flush=True)

    if not dry_run:
        session.commit()
    verb = "would change" if dry_run else "updated"
    print(f"  matched {matched:,} event_id(s); {changed:,} {verb}.")
    return changed, touched


def run(db_path: Path | None, dry_run: bool, skip_rollups: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"{prefix}Backfilling OpenCode variant → effort…", flush=True)

    # Idempotently ensure columns exist (ALTER ADD COLUMN) so this works even
    # against a DB the server hasn't started against yet.
    init_db()

    try:
        db_paths = _discover_db_paths(db_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    if not db_paths:
        print(
            "No OpenCode DB found (~/.local/share/opencode or ~/.opencode) — nothing to backfill."
        )
        return
    print(f"  source DB(s): {', '.join(str(p) for p in db_paths)}", flush=True)

    pushes = _collect_pushes(db_paths)
    if not pushes:
        print("  no assistant messages parsed — nothing to backfill.")
        return

    with Session(engine) as session:
        changed, touched = phase_b_effort(session, pushes, dry_run)
        print(f"{prefix}Phase B done — {changed:,} event(s) updated.", flush=True)
        if not dry_run and touched and not skip_rollups:
            n = backfill_rollups(sorted(touched))
            print(
                f"{prefix}Phase C done — rollups rebuilt for {len(touched)} provider(s) from {n:,} event(s).",
                flush=True,
            )
        elif skip_rollups:
            print("Phase C skipped (--skip-rollups).", flush=True)
        elif dry_run:
            print(
                f"{prefix}Phase C skipped (dry-run; would rebuild {len(touched)} provider(s)).",
                flush=True,
            )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Override path to opencode.db (default: discover ~/.local/share and ~/.opencode).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing (still runs init_db ALTERs to ensure columns exist).",
    )
    p.add_argument("--skip-rollups", action="store_true", help="Skip Phase C (rollup rebuild).")
    args = p.parse_args(argv)
    try:
        run(
            db_path=Path(args.db) if args.db else None,
            dry_run=args.dry_run,
            skip_rollups=args.skip_rollups,
        )
    except SystemExit as exc:
        return int(exc.code or 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
