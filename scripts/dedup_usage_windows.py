#!/usr/bin/env python3
"""Collapse duplicate usage_windows rows spawned by jittery provider reset_at.

A provider whose ``resets_at`` oscillates by sub-second/sub-minute amounts
between polls (e.g. Anthropic's weekly window bounces ~±2s around the true
boundary) used to trip the window-close guard on every upward bounce. Because
``window_end`` carries microsecond precision, each bounce slipped past the
``usage_windows`` UNIQUE constraint and wrote a fresh row — flooding the archive
(observed: 12k+ "weekly" rows for one account, ~1.5k per real week). The
forecast tab's "Past windows" card then renders the same week dozens of times.

The window-close guard is fixed in ``app/services/window_closer.py`` (a real
rollover must advance reset_at by ~half a duration). This one-shot migration
cleans up rows written before that fix. It is provider-agnostic — every
``(provider_id, account_id, window_type)`` group is scanned.

Per group, rows are clustered by ``window_end`` *proximity* (not by truncating
the timestamp — jitter straddles minute and hour marks, so fixed-grain bucketing
would split one real window in two). Real windows are >= one full duration apart,
so rows within ``WINDOW_DURATION / 2`` of each other belong to the same logical
window. For each cluster:

  * **in-progress** (``window_end`` in the future) -> delete every row. The
    window has not actually closed; the fixed guard will archive it correctly
    from usage_events when it really rolls over.
  * **closed** -> keep the all-up rollup row (``model_id=''``, ``sidecar_id=''``)
    with the highest ``msgs`` (the most-complete snapshot ~ the final total).
    Its exact ``(window_start, window_end)`` is canonical; keep every grain row
    (per-model / per-sidecar / cross-product) sharing that tuple and delete the
    rest. (query_window_history joins grains by ``(window_start, window_end)``,
    so mixing tuples would orphan the splits.)

Window types not in WINDOW_DURATION are left untouched (no authoritative
duration to cluster by).

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/dedup_usage_windows.py --dry-run
  # eyeball the planned deletions, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/dedup_usage_windows.py --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure repo root is on sys.path when the script is invoked directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, col, delete, select  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import UsageWindow  # noqa: E402
from app.services.window_closer import WINDOW_DURATION  # noqa: E402


def _naive(dt: datetime) -> datetime:
    """Drop tzinfo for naive/aware-agnostic comparison (SQLite stores naive UTC)."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _cluster_by_proximity(rows: list[UsageWindow], tol_seconds: float) -> list[list[UsageWindow]]:
    """Single-linkage cluster rows by window_end; gap > tol starts a new cluster.

    Safe because a logical window's rows share a window_end within a few seconds
    of jitter, while distinct real windows are >= one full duration apart (and
    tol is half a duration).
    """
    ordered = sorted(rows, key=lambda r: _naive(r.window_end))
    clusters: list[list[UsageWindow]] = []
    for r in ordered:
        end = _naive(r.window_end)
        if clusters and (end - _naive(clusters[-1][-1].window_end)).total_seconds() <= tol_seconds:
            clusters[-1].append(r)
        else:
            clusters.append([r])
    return clusters


def plan_group_deletions(
    rows: list[UsageWindow], *, duration_seconds: float, now: datetime
) -> list[UsageWindow]:
    """Return the rows to delete for one (provider, account, window_type) group.

    Pure (no DB): testable by passing in-memory UsageWindow instances.
    """
    tol = duration_seconds / 2
    now_naive = _naive(now)
    to_delete: list[UsageWindow] = []

    for cluster in _cluster_by_proximity(rows, tol):
        if len(cluster) == 1:
            # A lone row is either a clean closed window or a lone in-progress
            # one; only delete it if it's in-progress.
            if _naive(cluster[0].window_end) > now_naive:
                to_delete.append(cluster[0])
            continue

        # In-progress cluster (boundary in the future) -> every row is spurious.
        if max(_naive(r.window_end) for r in cluster) > now_naive:
            to_delete.extend(cluster)
            continue

        # Closed cluster -> pick the canonical (window_start, window_end) tuple
        # from the all-up rollup row with the most messages; fall back to the
        # overall max-msgs row if no rollup grain is present.
        rollups = [r for r in cluster if r.model_id == "" and r.sidecar_id == ""]
        pool = rollups or cluster
        canonical = max(pool, key=lambda r: r.msgs)
        keep_key = (_naive(canonical.window_start), _naive(canonical.window_end))
        for r in cluster:
            if (_naive(r.window_start), _naive(r.window_end)) != keep_key:
                to_delete.append(r)

    return to_delete


def _distinct_groups(session: Session) -> list[tuple[str, str, str]]:
    rows = session.exec(
        select(UsageWindow.provider_id, UsageWindow.account_id, UsageWindow.window_type).distinct()
    ).all()
    return [(p, a, w) for (p, a, w) in rows]


def migrate(apply: bool) -> int:
    now = datetime.now(UTC)
    total_before = 0
    total_deleted = 0
    skipped_types: set[str] = set()

    with Session(engine) as session:
        groups = _distinct_groups(session)
        print(f"Scanning {len(groups)} (provider, account, window_type) group(s)...\n")

        for provider_id, account_id, window_type in sorted(groups):
            if window_type not in WINDOW_DURATION:
                skipped_types.add(window_type)
                continue

            rows = session.exec(
                select(UsageWindow).where(
                    UsageWindow.provider_id == provider_id,
                    UsageWindow.account_id == account_id,
                    UsageWindow.window_type == window_type,
                )
            ).all()
            total_before += len(rows)

            duration_seconds = WINDOW_DURATION[window_type].total_seconds()
            doomed = plan_group_deletions(list(rows), duration_seconds=duration_seconds, now=now)
            if not doomed:
                continue

            kept = len(rows) - len(doomed)
            print(
                f"  {provider_id}/{account_id}/{window_type}: "
                f"{len(rows)} -> {kept} rows (delete {len(doomed)})"
            )
            total_deleted += len(doomed)

            if apply:
                doomed_ids = [r.id for r in doomed if r.id is not None]
                for i in range(0, len(doomed_ids), 500):
                    chunk = doomed_ids[i : i + 500]
                    session.exec(delete(UsageWindow).where(col(UsageWindow.id).in_(chunk)))

        if skipped_types:
            print(
                f"\nSkipped window types with no authoritative duration: "
                f"{', '.join(sorted(skipped_types))}"
            )

        if not apply:
            print(
                f"\nDry run — no changes written. Would delete {total_deleted} of "
                f"{total_before} row(s). Re-run with --apply to execute."
            )
            return 0

        session.commit()
        print(
            f"\nDeleted {total_deleted} of {total_before} row(s); "
            f"{total_before - total_deleted} remain."
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Preview deletions, write nothing.")
    g.add_argument("--apply", action="store_true", help="Execute the migration.")
    args = p.parse_args()
    return migrate(apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
