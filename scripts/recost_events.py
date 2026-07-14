#!/usr/bin/env python3
"""Recompute cost_usd on usage_events and rebuild derived cost tables.

Use after a provider_pricing seed change so that existing events pick up the
new rates. Three passes are run in sequence:

  Phase B — update usage_events.cost_usd where the recomputed value differs.
  Phase C — delete and rebuild usage_period_rollup for the affected providers.
  Phase D — delete and rebuild usage_windows for the affected providers.

OpenCode events (provider_id 'opencode', 'opencode-free', and any other
'opencode-*' sub-provider, e.g. 'opencode-byok') carry an authoritative cost
supplied by the provider; they are always skipped. Error events (kind !=
'message') never had a cost and are always skipped.

Note on effective_from: cost_calculator only applies a pricing row when
effective_from <= event.ts.date(). If the new seed rows are dated today,
events from before today will still compute to 0.0 for those model_ids
unless you also backdate effective_from in pricing_seed.py.

Examples:
  # Recompute chatgpt only
  python scripts/recost_events.py --provider chatgpt

  # Preview without writing
  python scripts/recost_events.py --provider chatgpt --dry-run

  # Recompute several providers, skipping the window archive rebuild
  python scripts/recost_events.py --provider chatgpt --provider gemini --skip-windows

  # Recompute all providers, only events from a date onward (Phase B only)
  python scripts/recost_events.py --all --since 2025-08-01

  # Full rebuild for all providers
  python scripts/recost_events.py --all
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, delete, select  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import UsageEvent, UsagePeriodRollup, UsageWindow  # noqa: E402
from app.services.cost_calculator import compute_event_cost  # noqa: E402
from app.services.period_rollups import update_rollups_for_event  # noqa: E402
from app.services.window_closer import close_window  # noqa: E402

# "opencode" itself plus every opencode-* sub-provider (opencode-free,
# opencode-byok, opencode-openrouter, opencode-ollama, and any future
# opencode-<slug> derived by map_opencode_provider_id) — matched via a LIKE
# prefix below rather than an exact-id tuple so new siblings are covered
# automatically.
_SKIP_PROVIDER_PREFIX = "opencode"

# Phase D commits and expunges every _WINDOW_BATCH window rebuilds. close_window()
# loads events and writes window rows into the session, so without periodic
# expunge_all() the identity map accumulates the whole event + window set and
# every select()'s autoflush degrades to O(n) — quadratic over the run, which
# hangs on large providers (anthropic: ~90k events / ~11k windows) while
# finishing fine on small ones. Recycling the map keeps each batch flat.
_WINDOW_BATCH = 200


def _event_scope(stmt, providers: list[str] | None, since: date | None):
    stmt = stmt.where(UsageEvent.kind == "message")
    stmt = stmt.where(
        UsageEvent.provider_id.notlike(f"{_SKIP_PROVIDER_PREFIX}%")  # type: ignore[attr-defined]
    )
    if providers:
        stmt = stmt.where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
    if since:
        stmt = stmt.where(UsageEvent.ts >= datetime(since.year, since.month, since.day))
    return stmt


def phase_b_recost(
    session: Session,
    providers: list[str] | None,
    since: date | None,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Recompute cost_usd on usage_events. Returns (updated, unchanged, zeroed)."""
    stmt = _event_scope(select(UsageEvent).order_by(UsageEvent.ts), providers, since)
    events = session.exec(stmt).all()
    print(f"Phase B — examining {len(events):,} event(s)…", flush=True)

    updated = unchanged = zeroed = 0
    for i, ev in enumerate(events, 1):
        new_cost = compute_event_cost(
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
        if abs(new_cost - ev.cost_usd) > 1e-9:
            if new_cost == 0.0:
                zeroed += 1
            else:
                updated += 1
            if not dry_run:
                ev.cost_usd = new_cost
                session.add(ev)
        else:
            unchanged += 1

        if i % 1000 == 0:
            if not dry_run:
                session.commit()
            print(f"  …{i:,}", flush=True)

    if not dry_run:
        session.commit()
    return updated, unchanged, zeroed


def phase_c_rollups(
    session: Session,
    providers: list[str] | None,
    dry_run: bool,
) -> int:
    """Rebuild usage_period_rollup for the affected providers. Returns events processed."""
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
        # Own the commit: update_rollups_for_event leaves the last partial batch
        # pending, and a prior version relied on Phase D's commit to flush it —
        # so --skip-windows silently dropped the rebuild.
        session.commit()
    return len(events)


def phase_d_windows(
    session: Session,
    providers: list[str] | None,
    dry_run: bool,
) -> int:
    """Rebuild usage_windows for the affected providers. Returns window-identities rebuilt."""
    if providers:
        existing = session.exec(
            select(UsageWindow).where(UsageWindow.provider_id.in_(providers))  # type: ignore[attr-defined]
        ).all()
    else:
        existing = session.exec(select(UsageWindow)).all()

    # Group by the 5-tuple window identity; prefer the all-grains row for limit/pct.
    window_index: dict[tuple, tuple[float | None, float | None]] = {}
    for w in existing:
        key = (w.provider_id, w.account_id, w.window_type, w.window_start, w.window_end)
        if w.model_id == "" and w.sidecar_id == "":
            window_index[key] = (w.limit_value, w.pct_used)
        else:
            window_index.setdefault(key, (w.limit_value, w.pct_used))

    print(f"Phase D — rebuilding {len(window_index):,} window(s)…", flush=True)

    if not dry_run:
        if providers:
            session.exec(
                delete(UsageWindow).where(UsageWindow.provider_id.in_(providers))  # type: ignore[attr-defined]
            )
        else:
            session.exec(delete(UsageWindow))
        session.commit()
        # Drop any rows the earlier phases loaded so the rebuild map starts empty.
        session.expunge_all()

        for i, ((pid, aid, wtype, start, end), (lv, pu)) in enumerate(window_index.items(), 1):
            close_window(
                session,
                provider_id=pid,
                account_id=aid,
                window_type=wtype,
                window_start=start,
                window_end=end,
                limit_value=lv,
                pct_used=pu,
            )
            if i % _WINDOW_BATCH == 0:
                session.commit()
                session.expunge_all()  # bound the identity map — see _WINDOW_BATCH
                print(f"  …{i:,}/{len(window_index):,}", flush=True)
        session.commit()
        session.expunge_all()

    return len(window_index)


def run(
    providers: list[str] | None,
    since: date | None,
    dry_run: bool,
    skip_rollups: bool,
    skip_windows: bool,
) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    scope = "all providers" if providers is None else ", ".join(providers)
    print(f"{prefix}Re-costing events for: {scope}", flush=True)

    with Session(engine) as session:
        updated, unchanged, zeroed = phase_b_recost(session, providers, since, dry_run)
        print(
            f"{prefix}Phase B done — {updated + unchanged + zeroed:,} event(s): "
            f"{updated} updated, {unchanged} unchanged, {zeroed} newly-zeroed.",
            flush=True,
        )

        if not skip_rollups:
            n_events = phase_c_rollups(session, providers, dry_run)
            print(f"{prefix}Phase C done — rollups rebuilt from {n_events:,} event(s).", flush=True)
        else:
            print("Phase C skipped (--skip-rollups).", flush=True)

        if not skip_windows:
            n_windows = phase_d_windows(session, providers, dry_run)
            print(f"{prefix}Phase D done — {n_windows:,} window(s) rebuilt.", flush=True)
        else:
            print("Phase D skipped (--skip-windows).", flush=True)


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
        help="Provider id to re-cost (repeatable). Use --all to target every provider.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Re-cost events for every provider.",
    )
    p.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Only re-cost events on or after this date (Phase B). "
        "Phases C and D always rebuild all events for the affected providers.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    p.add_argument(
        "--skip-rollups",
        action="store_true",
        help="Skip Phase C (usage_period_rollup rebuild).",
    )
    p.add_argument(
        "--skip-windows",
        action="store_true",
        help="Skip Phase D (usage_windows rebuild).",
    )
    args = p.parse_args()

    if not args.providers and not args.all:
        p.error("supply --provider <id> (repeatable) or --all")

    since: date | None = None
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            p.error(f"--since must be YYYY-MM-DD, got: {args.since!r}")

    providers = args.providers if not args.all else None
    run(
        providers=providers,
        since=since,
        dry_run=args.dry_run,
        skip_rollups=args.skip_rollups,
        skip_windows=args.skip_windows,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
