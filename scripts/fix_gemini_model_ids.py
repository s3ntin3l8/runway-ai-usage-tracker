#!/usr/bin/env python3
"""One-time migration: rename legacy Gemini model_ids and re-cost.

Before this change the sidecar Gemini extractor collapsed every variant
("gemini-2.5-pro", "gemini-3-pro-preview", …) into the same three buckets
("pro", "flash", "flash-lite"), and the pricing seed billed them at the
old (incorrect) cache-read rates. The extractor now emits versioned ids
("pro-2.5", "pro-3.1-preview", "flash-2.5", "flash-lite-2.5") that look
up the correct rates per https://ai.google.dev/gemini-api/docs/pricing.

This script repairs existing rows so historical cost reflects the
official rates and so the dashboard's per-model breakdown distinguishes
2.5 from 3.x going forward.

Assumption: every historical "pro" / "flash" / "flash-lite" event is
Gemini 2.5. Justification: the extractor has only ever emitted those
three buckets, so 3.x cannot be disambiguated in historical data.

Four passes are run in sequence:

  Phase A — UPDATE usage_events.model_id: pro → pro-2.5, flash → flash-2.5,
           flash-lite → flash-lite-2.5 (only provider_id="gemini").
  Phase B — recompute usage_events.cost_usd via cost_calculator (picks up
           the corrected cache-read rates on the new pricing rows).
  Phase C — delete + rebuild usage_period_rollup for gemini.
  Phase D — delete + rebuild usage_windows for gemini.

Idempotency: after Phase A the WHERE predicate matches zero rows, so
re-running is a no-op. Phase B is naturally idempotent (writes only when
the cost changed). Phases C and D delete then rebuild, so re-running
produces the same result.

Examples:
  # Preview without writing
  python scripts/fix_gemini_model_ids.py --dry-run

  # Apply
  python scripts/fix_gemini_model_ids.py

  # Skip the slow window rebuild (rarely useful)
  python scripts/fix_gemini_model_ids.py --skip-windows
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
from app.models.db import UsageEvent, UsagePeriodRollup, UsageWindow  # noqa: E402
from app.services.cost_calculator import compute_event_cost  # noqa: E402
from app.services.period_rollups import update_rollups_for_event  # noqa: E402
from app.services.pricing_seed import seed_pricing_table  # noqa: E402
from app.services.window_closer import close_window  # noqa: E402

_PROVIDER = "gemini"
_RENAME_MAP = {
    "pro": "pro-2.5",
    "flash": "flash-2.5",
    "flash-lite": "flash-lite-2.5",
}


def phase_a_rename(session: Session, dry_run: bool) -> dict[str, int]:
    """Rename legacy gemini model_ids on usage_events. Returns per-id counts."""
    counts: dict[str, int] = {}
    for old, new in _RENAME_MAP.items():
        stmt = (
            select(UsageEvent)
            .where(UsageEvent.provider_id == _PROVIDER)
            .where(UsageEvent.model_id == old)
        )
        events = session.exec(stmt).all()
        counts[old] = len(events)
        if not dry_run:
            for ev in events:
                ev.model_id = new
                session.add(ev)
            session.commit()
    total = sum(counts.values())
    print(
        f"Phase A — renamed {total:,} event(s): "
        + ", ".join(f"{old}→{_RENAME_MAP[old]}: {n:,}" for old, n in counts.items()),
        flush=True,
    )
    return counts


def phase_b_recost(session: Session, dry_run: bool) -> tuple[int, int]:
    """Recompute cost_usd for all gemini events. Returns (updated, unchanged)."""
    stmt = (
        select(UsageEvent)
        .where(UsageEvent.kind == "message")
        .where(UsageEvent.provider_id == _PROVIDER)
        .order_by(UsageEvent.ts)
    )
    events = session.exec(stmt).all()
    print(f"Phase B — recosting {len(events):,} event(s)…", flush=True)

    updated = unchanged = 0
    cost_delta = 0.0
    for i, ev in enumerate(events, 1):
        # In dry-run mode Phase A doesn't persist the rename, so apply it
        # in-memory here to preview the real Phase B outcome accurately.
        lookup_model_id = _RENAME_MAP.get(ev.model_id, ev.model_id)
        new_cost = compute_event_cost(
            session,
            provider_id=ev.provider_id,
            model_id=lookup_model_id,
            ts=ev.ts,
            tokens_input=ev.tokens_input,
            tokens_output=ev.tokens_output,
            tokens_cache_read=ev.tokens_cache_read,
            tokens_cache_create=ev.tokens_cache_create,
            tokens_reasoning=ev.tokens_reasoning,
        )
        if abs(new_cost - ev.cost_usd) > 1e-9:
            updated += 1
            cost_delta += new_cost - ev.cost_usd
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
    print(
        f"Phase B — total cost_usd delta across re-costed events: {cost_delta:+.4f}",
        flush=True,
    )
    return updated, unchanged


def phase_c_rollups(session: Session, dry_run: bool) -> int:
    """Delete + rebuild usage_period_rollup for gemini. Returns events processed."""
    ev_stmt = (
        select(UsageEvent)
        .where(UsageEvent.kind == "message")
        .where(UsageEvent.provider_id == _PROVIDER)
        .order_by(UsageEvent.ts)
    )
    events = session.exec(ev_stmt).all()
    print(f"Phase C — rebuilding rollups from {len(events):,} event(s)…", flush=True)

    if not dry_run:
        session.exec(delete(UsagePeriodRollup).where(UsagePeriodRollup.provider_id == _PROVIDER))
        session.commit()
        for i, ev in enumerate(events, 1):
            update_rollups_for_event(session, ev)
            if i % 1000 == 0:
                print(f"  …{i:,}", flush=True)
        session.commit()
    return len(events)


def phase_d_windows(session: Session, dry_run: bool) -> int:
    """Delete + rebuild usage_windows for gemini. Returns window-identities rebuilt."""
    existing = session.exec(select(UsageWindow).where(UsageWindow.provider_id == _PROVIDER)).all()

    window_index: dict[tuple, tuple[float | None, float | None]] = {}
    for w in existing:
        key = (w.provider_id, w.account_id, w.window_type, w.window_start, w.window_end)
        if w.model_id == "" and w.sidecar_id == "":
            window_index[key] = (w.limit_value, w.pct_used)
        else:
            window_index.setdefault(key, (w.limit_value, w.pct_used))

    print(f"Phase D — rebuilding {len(window_index):,} window(s)…", flush=True)

    if not dry_run:
        session.exec(delete(UsageWindow).where(UsageWindow.provider_id == _PROVIDER))
        session.commit()

        for (pid, aid, wtype, start, end), (lv, pu) in window_index.items():
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
        session.commit()

    return len(window_index)


def run(dry_run: bool, skip_windows: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"{prefix}Migrating Gemini model_ids and re-costing.", flush=True)

    with Session(engine) as session:
        # Always seed (even in dry-run) so Phase B's preview is accurate.
        # The seeder is purely additive — only inserts missing
        # (provider, model, effective_from) tuples; existing rows are
        # untouched and new ids are inert until Phase A renames events to them.
        inserted = seed_pricing_table(session)
        print(f"Pricing seed — inserted {inserted} new row(s).", flush=True)

        phase_a_rename(session, dry_run)

        updated, unchanged = phase_b_recost(session, dry_run)
        print(
            f"{prefix}Phase B done — {updated:,} cost(s) updated, {unchanged:,} unchanged.",
            flush=True,
        )

        n_events = phase_c_rollups(session, dry_run)
        print(f"{prefix}Phase C done — rollups rebuilt from {n_events:,} event(s).", flush=True)

        if not skip_windows:
            n_windows = phase_d_windows(session, dry_run)
            print(f"{prefix}Phase D done — {n_windows:,} window(s) rebuilt.", flush=True)
        else:
            print("Phase D skipped (--skip-windows).", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    p.add_argument(
        "--skip-windows",
        action="store_true",
        help="Skip Phase D (usage_windows rebuild).",
    )
    args = p.parse_args()

    run(dry_run=args.dry_run, skip_windows=args.skip_windows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
