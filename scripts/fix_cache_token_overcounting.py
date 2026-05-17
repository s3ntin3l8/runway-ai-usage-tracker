#!/usr/bin/env python3
"""One-time fix for Gemini + ChatGPT cache token double-counting.

Earlier versions of the sidecar Gemini and ChatGPT extractors stored each
event's raw `input_tokens` value (which for those two providers is inclusive
of cached tokens) in `usage_events.tokens_input`, AND separately stored the
same cached count in `tokens_cache_read`. Result: cache tokens were counted
twice — once inside `tokens_input` and once in `tokens_cache_read` — and
cost was over-billed by the same amount.

The extractors were corrected to subtract cached from input at extraction
time. This script repairs historical rows already in the database.

Four passes are run in sequence:

  Phase A — UPDATE usage_events.tokens_input = MAX(0, tokens_input - tokens_cache_read)
           for gemini + chatgpt message events with cached > 0.
  Phase B — recompute usage_events.cost_usd via cost_calculator.
  Phase C — delete + rebuild usage_period_rollup for the affected providers.
  Phase D — delete + rebuild usage_windows for the affected providers.

Idempotency: Phase A subtracts `tokens_cache_read` once. Running twice
without re-ingesting old corrupted events would subtract again and zero
out the input — DO NOT re-run after a successful pass. Use --dry-run first.

Examples:
  # Preview both providers
  python scripts/fix_cache_token_overcounting.py --dry-run

  # Apply to both providers (default scope)
  python scripts/fix_cache_token_overcounting.py

  # Limit to one provider
  python scripts/fix_cache_token_overcounting.py --provider gemini
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
from app.services.window_closer import close_window  # noqa: E402

_DEFAULT_PROVIDERS = ("gemini", "chatgpt")


def phase_a_fix_tokens(
    session: Session,
    providers: list[str],
    dry_run: bool,
) -> tuple[int, int, int]:
    """Subtract tokens_cache_read from tokens_input. Returns (touched, before, after)."""
    stmt = (
        select(UsageEvent)
        .where(UsageEvent.kind == "message")
        .where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
        .where(UsageEvent.tokens_cache_read > 0)
    )
    events = session.exec(stmt).all()
    print(f"Phase A — examining {len(events):,} candidate event(s)…", flush=True)

    touched = 0
    sum_input_before = 0
    sum_input_after = 0
    for i, ev in enumerate(events, 1):
        new_input = max(0, ev.tokens_input - ev.tokens_cache_read)
        sum_input_before += ev.tokens_input
        sum_input_after += new_input
        if new_input != ev.tokens_input:
            touched += 1
            if not dry_run:
                ev.tokens_input = new_input
                session.add(ev)

        if i % 1000 == 0:
            if not dry_run:
                session.commit()
            print(f"  …{i:,}", flush=True)

    if not dry_run:
        session.commit()
    return touched, sum_input_before, sum_input_after


def phase_b_recost(
    session: Session,
    providers: list[str],
    dry_run: bool,
) -> tuple[int, int]:
    """Recompute cost_usd. Returns (updated, unchanged)."""
    stmt = (
        select(UsageEvent)
        .where(UsageEvent.kind == "message")
        .where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
        .order_by(UsageEvent.ts)
    )
    events = session.exec(stmt).all()
    print(f"Phase B — recosting {len(events):,} event(s)…", flush=True)

    updated = unchanged = 0
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
    return updated, unchanged


def phase_c_rollups(
    session: Session,
    providers: list[str],
    dry_run: bool,
) -> int:
    """Rebuild usage_period_rollup for the affected providers. Returns events processed."""
    del_stmt = delete(UsagePeriodRollup).where(
        UsagePeriodRollup.provider_id.in_(providers)  # type: ignore[attr-defined]
    )
    ev_stmt = (
        select(UsageEvent)
        .where(UsageEvent.kind == "message")
        .where(UsageEvent.provider_id.in_(providers))  # type: ignore[attr-defined]
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
                print(f"  …{i:,}", flush=True)
    return len(events)


def phase_d_windows(
    session: Session,
    providers: list[str],
    dry_run: bool,
) -> int:
    """Rebuild usage_windows for the affected providers. Returns window-identities rebuilt."""
    existing = session.exec(
        select(UsageWindow).where(UsageWindow.provider_id.in_(providers))  # type: ignore[attr-defined]
    ).all()

    window_index: dict[tuple, tuple[float | None, float | None]] = {}
    for w in existing:
        key = (w.provider_id, w.account_id, w.window_type, w.window_start, w.window_end)
        if w.model_id == "" and w.sidecar_id == "":
            window_index[key] = (w.limit_value, w.pct_used)
        else:
            window_index.setdefault(key, (w.limit_value, w.pct_used))

    print(f"Phase D — rebuilding {len(window_index):,} window(s)…", flush=True)

    if not dry_run:
        session.exec(
            delete(UsageWindow).where(UsageWindow.provider_id.in_(providers))  # type: ignore[attr-defined]
        )
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


def run(
    providers: list[str],
    dry_run: bool,
    skip_windows: bool,
) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"{prefix}Fixing cache-token overcounting for: {', '.join(providers)}", flush=True)

    with Session(engine) as session:
        touched, before, after = phase_a_fix_tokens(session, providers, dry_run)
        print(
            f"{prefix}Phase A done — {touched:,} event(s) touched. "
            f"tokens_input sum: {before:,} → {after:,} (Δ = -{before - after:,})",
            flush=True,
        )

        updated, unchanged = phase_b_recost(session, providers, dry_run)
        print(
            f"{prefix}Phase B done — {updated:,} cost(s) updated, {unchanged:,} unchanged.",
            flush=True,
        )

        n_events = phase_c_rollups(session, providers, dry_run)
        print(f"{prefix}Phase C done — rollups rebuilt from {n_events:,} event(s).", flush=True)

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
        help=(
            "Provider id to fix (repeatable). Defaults to "
            f"{', '.join(_DEFAULT_PROVIDERS)} if omitted."
        ),
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

    providers = args.providers or list(_DEFAULT_PROVIDERS)
    run(providers=providers, dry_run=args.dry_run, skip_windows=args.skip_windows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
