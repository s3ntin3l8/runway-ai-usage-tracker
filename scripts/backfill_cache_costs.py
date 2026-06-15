#!/usr/bin/env python3
"""Backfill per-component cost columns on usage_events and rebuild rollups.

Issue #73 added cost_input / cost_output / cost_cache_read / cost_cache_create to
usage_events and usage_period_rollup so cost views can subtract the cache portion.
Existing rows default these to 0, so this script populates them from each event's
stored token counts × the price row in effect at the event's timestamp.

  Phase B — write the four component columns on usage_events (cost_usd is left
            untouched: it stays the authoritative total, which for some providers
            like OpenCode is provider-supplied and not recomputed here).
  Phase C — delete and replay usage_period_rollup so its component sums match.

Unlike recost_events.py, OpenCode events are NOT skipped: we want best-effort
pricing-derived components for every provider (unpriced models simply yield 0).

Run against the dev DB or a snapshot — never the live prod DB while the server is
writing (SQLite is single-writer):

  APP_HOST=127.0.0.1 python scripts/backfill_cache_costs.py            # all providers
  APP_HOST=127.0.0.1 python scripts/backfill_cache_costs.py --dry-run
  APP_HOST=127.0.0.1 python scripts/backfill_cache_costs.py --provider anthropic
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, delete, select  # noqa: E402

from app.core.db import engine, init_db  # noqa: E402
from app.models.db import UsageEvent, UsagePeriodRollup  # noqa: E402
from app.services.cost_calculator import compute_event_cost_breakdown  # noqa: E402
from app.services.period_rollups import update_rollups_for_event  # noqa: E402


def _event_scope(stmt, providers: list[str] | None):
    stmt = stmt.where(UsageEvent.kind == "message")
    if providers:
        stmt = stmt.where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
    return stmt


def phase_b_components(session: Session, providers: list[str] | None, dry_run: bool) -> int:
    """Write the four cost-component columns on usage_events. Returns events changed."""
    events = session.exec(_event_scope(select(UsageEvent).order_by(UsageEvent.ts), providers)).all()
    print(f"Phase B — examining {len(events):,} event(s)…", flush=True)

    changed = 0
    for i, ev in enumerate(events, 1):
        b = compute_event_cost_breakdown(
            session,
            provider_id=ev.provider_id,
            model_id=ev.model_id,
            ts=ev.ts,
            tokens_input=ev.tokens_input,
            tokens_output=ev.tokens_output,
            tokens_cache_read=ev.tokens_cache_read,
            tokens_cache_create=ev.tokens_cache_create,
            tokens_reasoning=ev.tokens_reasoning,
        )
        if (
            abs(b.input - ev.cost_input) > 1e-9
            or abs(b.output - ev.cost_output) > 1e-9
            or abs(b.cache_read - ev.cost_cache_read) > 1e-9
            or abs(b.cache_create - ev.cost_cache_create) > 1e-9
        ):
            changed += 1
            if not dry_run:
                ev.cost_input = b.input
                ev.cost_output = b.output
                ev.cost_cache_read = b.cache_read
                ev.cost_cache_create = b.cache_create
                session.add(ev)
        if i % 1000 == 0:
            if not dry_run:
                session.commit()
            print(f"  …{i:,}", flush=True)

    if not dry_run:
        session.commit()
    return changed


def phase_c_rollups(session: Session, providers: list[str] | None, dry_run: bool) -> int:
    """Delete + replay usage_period_rollup so component sums match. Returns events replayed."""
    if providers:
        del_stmt = delete(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id.in_(providers)  # type: ignore[attr-defined]
        )
        ev_stmt = (
            select(UsageEvent)
            .where(UsageEvent.kind == "message")
            .where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
            .order_by(UsageEvent.ts)
        )
    else:
        del_stmt = delete(UsagePeriodRollup)
        ev_stmt = select(UsageEvent).where(UsageEvent.kind == "message").order_by(UsageEvent.ts)

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


def run(providers: list[str] | None, dry_run: bool, skip_rollups: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    scope = "all providers" if providers is None else ", ".join(providers)
    print(f"{prefix}Backfilling cost components for: {scope}", flush=True)

    # Idempotently ensure the new cost-component columns exist (ALTER ADD COLUMN)
    # so the script works on a DB that hasn't been started by the server yet.
    init_db()

    with Session(engine) as session:
        changed = phase_b_components(session, providers, dry_run)
        print(f"{prefix}Phase B done — {changed:,} event(s) updated.", flush=True)
        if not skip_rollups:
            n = phase_c_rollups(session, providers, dry_run)
            print(f"{prefix}Phase C done — rollups rebuilt from {n:,} event(s).", flush=True)
        else:
            print("Phase C skipped (--skip-rollups).", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--provider",
        action="append",
        default=[],
        dest="providers",
        metavar="ID",
        help="Provider id to backfill (repeatable). Default: every provider.",
    )
    p.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    p.add_argument("--skip-rollups", action="store_true", help="Skip Phase C (rollup rebuild).")
    args = p.parse_args()
    run(
        providers=args.providers or None,
        dry_run=args.dry_run,
        skip_rollups=args.skip_rollups,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
