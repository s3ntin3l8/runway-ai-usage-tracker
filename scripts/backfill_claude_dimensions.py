#!/usr/bin/env python3
"""Backfill Claude Code per-message dimensions onto already-ingested events.

The JSONL parser (scripts/sidecar_pkg/event_extractors/anthropic.py) used to
discard `effort`, `speed`, `service_tier`, `entrypoint`/`version`, the cache-write
TTL split (1h vs 5m — priced at different rates), and web-tool counts. Existing
`usage_events` rows ingested before that fix have these columns at their
defaults (0 / NULL), including `tokens_cache_create_1h`/`_5m`, so their
`cost_cache_create` was computed treating 100% of cache writes as 5-minute-TTL
— under-pricing any 1-hour-TTL cache write.

Unlike scripts/backfill_cache_costs.py (which recomputes purely from token
counts already stored on the row), the 1h/5m split isn't recoverable from the
DB alone: Anthropic events don't carry `raw_json` (no extractor sets it), so
the only source for the split is the **original local JSONL transcripts** —
meaning this script must run on the same host as `~/.claude/projects`, in the
Local topology (or wherever that machine's sidecar data was copied). It
re-parses those files with the current (fixed) parser, matches by `event_id`
against existing rows, and updates the new dimension columns + re-derives
`cost_cache_create`/`cost_usd` for anthropic events only.

Run against the dev DB or a snapshot — never the live prod DB while the server
is writing (SQLite is single-writer):

  APP_HOST=127.0.0.1 python scripts/backfill_claude_dimensions.py
  APP_HOST=127.0.0.1 python scripts/backfill_claude_dimensions.py --dry-run
  APP_HOST=127.0.0.1 python scripts/backfill_claude_dimensions.py --account you@example.com
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, delete, select  # noqa: E402

from app.core.db import engine, init_db  # noqa: E402
from app.models.db import UsageEvent, UsagePeriodRollup  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from app.services.cost_calculator import compute_event_cost_breakdown  # noqa: E402
from app.services.period_rollups import update_rollups_for_event  # noqa: E402
from scripts.sidecar import (  # noqa: E402
    _discover_anthropic_log_paths,
    discover_anthropic_email,
)
from scripts.sidecar_pkg.event_extractors.anthropic import parse_anthropic_events  # noqa: E402

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _parsed_by_event_id(account_id: str) -> dict[str, UsageEventPush]:
    log_paths = _discover_anthropic_log_paths()
    if not log_paths:
        print("No ~/.claude/projects JSONL files found on this host — nothing to backfill.")
        return {}
    print(f"Re-parsing {len(log_paths)} local transcript file(s)…", flush=True)
    parsed = parse_anthropic_events(log_paths, account_id=account_id, since=_EPOCH)
    return {p.event_id: p for p in parsed}


def phase_b_dimensions(
    session: Session, parsed: dict[str, UsageEventPush], account_id: str, dry_run: bool
) -> int:
    """Update dimension + cache-split columns and re-derive cache-create cost."""
    events = session.exec(
        select(UsageEvent)
        .where(UsageEvent.provider_id == "anthropic")
        .where(UsageEvent.account_id == account_id)
        .where(UsageEvent.kind == "message")
        .order_by(UsageEvent.ts)
    ).all()
    print(f"Phase B — examining {len(events):,} event(s) for account {account_id!r}…", flush=True)

    changed = 0
    matched = 0
    for i, ev in enumerate(events, 1):
        push = parsed.get(ev.event_id)
        if push is None:
            continue
        matched += 1

        breakdown = compute_event_cost_breakdown(
            session,
            provider_id=ev.provider_id,
            model_id=ev.model_id,
            ts=ev.ts,
            tokens_input=ev.tokens_input,
            tokens_output=ev.tokens_output,
            tokens_cache_read=ev.tokens_cache_read,
            tokens_cache_create=ev.tokens_cache_create,
            tokens_reasoning=ev.tokens_reasoning,
            tokens_cache_create_1h=push.tokens_cache_create_1h,
            tokens_cache_create_5m=push.tokens_cache_create_5m,
        )

        dirty = (
            ev.tokens_cache_create_1h != push.tokens_cache_create_1h
            or ev.tokens_cache_create_5m != push.tokens_cache_create_5m
            or ev.effort != push.effort
            or ev.speed != push.speed
            or ev.service_tier != push.service_tier
            or ev.entrypoint != push.entrypoint
            or ev.app_version != push.app_version
            or ev.web_search_requests != push.web_search_requests
            or ev.web_fetch_requests != push.web_fetch_requests
            or abs(ev.cost_cache_create - breakdown.cache_create) > 1e-9
        )
        if dirty:
            changed += 1
            if not dry_run:
                ev.tokens_cache_create_1h = push.tokens_cache_create_1h
                ev.tokens_cache_create_5m = push.tokens_cache_create_5m
                ev.effort = push.effort
                ev.speed = push.speed
                ev.service_tier = push.service_tier
                ev.entrypoint = push.entrypoint
                ev.app_version = push.app_version
                ev.web_search_requests = push.web_search_requests
                ev.web_fetch_requests = push.web_fetch_requests
                # cost_usd is never provider-supplied for anthropic (the parser
                # always leaves push.cost_usd None), so it's always safe to
                # re-derive the total alongside the recomputed component.
                cost_delta = breakdown.cache_create - ev.cost_cache_create
                ev.cost_cache_create = breakdown.cache_create
                ev.cost_usd = round(ev.cost_usd + cost_delta, 6)
                session.add(ev)
        if i % 1000 == 0:
            if not dry_run:
                session.commit()
            print(f"  …{i:,}", flush=True)

    if not dry_run:
        session.commit()
    print(f"  matched {matched:,} of {len(events):,} DB event(s) against local transcripts.")
    return changed


def phase_c_rollups(session: Session, account_id: str, dry_run: bool) -> int:
    """Delete + replay usage_period_rollup for this account's anthropic rows."""
    del_stmt = delete(UsagePeriodRollup).where(
        UsagePeriodRollup.provider_id == "anthropic",
        UsagePeriodRollup.account_id == account_id,
    )
    ev_stmt = (
        select(UsageEvent)
        .where(UsageEvent.provider_id == "anthropic")
        .where(UsageEvent.account_id == account_id)
        .where(UsageEvent.kind == "message")
        .order_by(UsageEvent.ts)
    )
    events = session.exec(ev_stmt).all()
    print(f"Phase C — rebuilding rollups from {len(events):,} event(s)…", flush=True)

    if not dry_run:
        session.exec(del_stmt)
        session.commit()
        for i, ev in enumerate(events, 1):
            update_rollups_for_event(session, ev)
            if i % 1000 == 0:
                session.commit()
                print(f"  …{i:,}", flush=True)
        session.commit()
    return len(events)


def run(account_id: str | None, dry_run: bool, skip_rollups: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    resolved_account = account_id or discover_anthropic_email() or "default"
    print(f"{prefix}Backfilling Claude Code dimensions for account: {resolved_account}", flush=True)

    # Idempotently ensure the new columns exist (ALTER ADD COLUMN) so this
    # works even against a DB the server hasn't started against yet.
    init_db()

    parsed = _parsed_by_event_id(resolved_account)
    if not parsed:
        return

    with Session(engine) as session:
        changed = phase_b_dimensions(session, parsed, resolved_account, dry_run)
        print(f"{prefix}Phase B done — {changed:,} event(s) updated.", flush=True)
        if not skip_rollups:
            n = phase_c_rollups(session, resolved_account, dry_run)
            print(f"{prefix}Phase C done — rollups rebuilt from {n:,} event(s).", flush=True)
        else:
            print("Phase C skipped (--skip-rollups).", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--account",
        default=None,
        metavar="ID",
        help="account_id to backfill. Default: auto-discover from ~/.claude credentials.",
    )
    p.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    p.add_argument("--skip-rollups", action="store_true", help="Skip Phase C (rollup rebuild).")
    args = p.parse_args()
    run(account_id=args.account, dry_run=args.dry_run, skip_rollups=args.skip_rollups)
    return 0


if __name__ == "__main__":
    sys.exit(main())
