#!/usr/bin/env python3
"""One-time migration: relabel mis-tagged Gemini 2.5 events as 3.x.

The prior migration ``fix_gemini_model_ids.py`` assumed every legacy
coarse-bucket gemini event was Gemini 2.5 and renamed ``pro``/``flash``/
``flash-lite`` to ``pro-2.5``/``flash-2.5``/``flash-lite-2.5``. For users
whose Gemini history only contains 3.x traffic that assumption mislabels
every event. This script corrects the mislabel by renaming::

    pro-2.5    → pro-3.1-preview
    flash-2.5  → flash-3-preview

then recomputes costs against the newly added 3.x pricing rows
(``app/services/pricing_seed.py``) and rebuilds the rollup + window tables
so derived totals stay consistent with the underlying events.

**Safety:** by default this only runs when the local Gemini CLI session
files at ``~/.gemini/tmp/*/chats/*.jsonl`` show **no** 2.5 traffic. If any
``"model":"gemini-2.5-*"`` line is found, the script aborts — running it
would silently mislabel real 2.5 events. Pass ``--force`` to skip that
check (only do this if you are sure the deleted/archived sessions had no
2.5 traffic either).

Phases (mirrors ``fix_gemini_model_ids.py``):

  Phase A — UPDATE usage_events.model_id for provider_id="gemini":
           pro-2.5 → pro-3.1-preview, flash-2.5 → flash-3-preview.
  Phase B — recompute usage_events.cost_usd via cost_calculator.
  Phase C — delete + rebuild usage_period_rollup for gemini.
  Phase D — delete + rebuild usage_windows for gemini.

Idempotent: after Phase A the WHERE predicate matches zero rows.

Examples:
  # Preview without writing (default)
  python scripts/fix_gemini_3x_relabel.py

  # Apply
  python scripts/fix_gemini_3x_relabel.py --apply

  # Bypass the 2.5-detection pre-flight check
  python scripts/fix_gemini_3x_relabel.py --apply --force
"""

from __future__ import annotations

import argparse
import glob
import os
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
    "pro-2.5": "pro-3.1-preview",
    "flash-2.5": "flash-3-preview",
}
_CHAT_GLOB = os.path.expanduser("~/.gemini/tmp/*/chats/*.jsonl")


def preflight_check_no_2_5_traffic() -> tuple[bool, list[str]]:
    """Scan local Gemini CLI session files for any 2.5 model strings.

    Returns ``(is_safe, evidence)``. ``is_safe`` is True when no
    ``"model":"gemini-2.5-..."`` line is found. ``evidence`` contains up
    to 5 file paths where 2.5 was detected (for the error message).
    """
    matches: list[str] = []
    for path in glob.glob(_CHAT_GLOB):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if '"model":"gemini-2.5' in line.replace(" ", ""):
                        matches.append(path)
                        break
        except OSError:
            continue
        if len(matches) >= 5:
            break
    return (len(matches) == 0, matches)


def phase_a_rename(session: Session, dry_run: bool) -> dict[str, int]:
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
        # In dry-run Phase A doesn't persist, so apply the rename in-memory
        # for an accurate preview.
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


def run(dry_run: bool, skip_windows: bool, force: bool) -> int:
    prefix = "[DRY-RUN] " if dry_run else ""

    if not force:
        safe, evidence = preflight_check_no_2_5_traffic()
        if not safe:
            print(
                "ABORT: found Gemini 2.5 traffic in local CLI sessions. Relabeling\n"
                "would mislabel real 2.5 events as 3.x. Re-run with --force only\n"
                "if you are certain no 2.5 events were ingested into the DB.\n"
                "Evidence (first matches):",
                file=sys.stderr,
            )
            for p in evidence:
                print(f"  {p}", file=sys.stderr)
            return 2
        print("Pre-flight — no Gemini 2.5 traffic found in local sessions. OK.", flush=True)
    else:
        print("Pre-flight skipped (--force).", flush=True)

    print(f"{prefix}Relabeling Gemini 2.5 → 3.x and recosting.", flush=True)

    with Session(engine) as session:
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

    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Default is dry-run.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip the 'no 2.5 traffic in local sessions' pre-flight check.",
    )
    p.add_argument(
        "--skip-windows",
        action="store_true",
        help="Skip Phase D (usage_windows rebuild).",
    )
    args = p.parse_args()

    return run(dry_run=not args.apply, skip_windows=args.skip_windows, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
