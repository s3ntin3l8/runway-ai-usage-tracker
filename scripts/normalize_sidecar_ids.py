#!/usr/bin/env python3
"""Collapse drifting sidecar ids onto their normalized form and merge duplicates.

A host that flips between its FQDN and its `.local`/short name (e.g.
`macbook.local` ⇄ `Macbook.in.example.de`) registers as two sidecars and splits
its history. This one-shot migration retags every stored row onto the normalized
id (``app.services.account_identity.normalize_sidecar_id`` — lowercased first DNS
label), then:

  * usage_events / latest_usage  → plain retag (sidecar_id is not in their
    identity unique-constraint, so this never collides),
  * usage_period_rollup          → rebuilt from the now-retagged events, scoped
    to the affected providers (reuses scripts/backfill_rollups.py:backfill),
  * usage_windows                → per-grain merge (sum the additive totals when
    two names collapse into one closed-window row),
  * sidecar_registry             → merged (sum counts, keep min/max seen, keep
    the latest row's metadata + any custom name).

Bare, already-lowercase short names (`mgmt`, `dev-01`, …) normalize to
themselves, so they are left untouched.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/normalize_sidecar_ids.py --dry-run
  # eyeball the planned folds, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/normalize_sidecar_ids.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root is on sys.path when the script is invoked directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select, update  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import (  # noqa: E402
    LatestUsage,
    SidecarRegistry,
    UsageEvent,
    UsageWindow,
)
from app.services.account_identity import normalize_sidecar_id  # noqa: E402
from scripts.backfill_rollups import backfill  # noqa: E402

# Additive numeric columns on a closed-window row (mirror window_closer totals).
_WINDOW_SUMS = (
    "msgs",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_create",
    "tokens_reasoning",
    "cost_usd",
)


def _distinct_sidecar_ids(session: Session) -> set[str]:
    ids: set[str] = set()
    for model in (UsageEvent, LatestUsage, UsageWindow, SidecarRegistry):
        rows = session.exec(select(model.sidecar_id).distinct()).all()  # type: ignore[attr-defined]
        ids.update(r for r in rows if r is not None)
    return ids


def _build_mapping(ids: set[str]) -> dict[str, str]:
    """src -> normalized target, only where they differ."""
    return {sid: normalize_sidecar_id(sid) for sid in ids if normalize_sidecar_id(sid) != sid}


def _window_key(row: UsageWindow, sidecar_id: str) -> tuple:
    return (
        row.provider_id,
        row.account_id,
        row.window_type,
        row.window_end,
        row.model_id,
        sidecar_id,
    )


def _merge_windows(session: Session, mapping: dict[str, str]) -> int:
    """Collapse source windows onto their normalized grain. Returns rows folded."""
    windows = session.exec(select(UsageWindow)).all()
    index: dict[tuple, UsageWindow] = {_window_key(w, w.sidecar_id): w for w in windows}
    folded = 0
    for w in windows:
        if w.sidecar_id not in mapping:
            continue
        tgt = mapping[w.sidecar_id]
        tgt_key = _window_key(w, tgt)
        existing = index.get(tgt_key)
        if existing is None:
            # No target row for this grain yet — plain retag.
            w.sidecar_id = tgt
            index[tgt_key] = w
        else:
            # A target row already holds this grain — fold w's totals into it.
            if w.msgs > existing.msgs:
                existing.limit_value = w.limit_value
                existing.pct_used = w.pct_used
            for col in _WINDOW_SUMS:
                setattr(existing, col, getattr(existing, col) + getattr(w, col))
            session.delete(w)
            folded += 1
    return folded


def _merge_registry(session: Session, mapping: dict[str, str]) -> int:
    """Merge registry rows that normalize to the same id. Returns rows removed."""
    rows = session.exec(select(SidecarRegistry)).all()
    groups: dict[str, list[SidecarRegistry]] = {}
    for r in rows:
        groups.setdefault(normalize_sidecar_id(r.sidecar_id), []).append(r)

    removed = 0
    for norm, grp in groups.items():
        if len(grp) == 1 and grp[0].sidecar_id == norm:
            continue  # already canonical, nothing to merge
        primary = max(grp, key=lambda r: r.last_seen)  # latest metadata wins
        named = next(
            (r for r in sorted(grp, key=lambda r: r.last_seen, reverse=True) if r.custom_name), None
        )
        tagged = next(
            (r for r in sorted(grp, key=lambda r: r.last_seen, reverse=True) if r.tags_json), None
        )
        merged = {
            "hostname": norm,
            "custom_name": named.custom_name if named else None,
            "tags_json": tagged.tags_json if tagged else None,
            "last_seen": max(r.last_seen for r in grp),
            "first_seen": min(r.first_seen for r in grp),
            "last_ip": primary.last_ip,
            "error_count": sum(r.error_count for r in grp),
            "ingest_count": sum(r.ingest_count for r in grp),
            "sidecar_version": primary.sidecar_version,
            "os_platform": primary.os_platform,
            "self_update_capable": primary.self_update_capable,
            "recent_logs": primary.recent_logs,
            "collection_enabled": primary.collection_enabled,
        }
        canonical = next((r for r in grp if r.sidecar_id == norm), None)
        for r in grp:
            if r is not canonical:
                session.delete(r)
                removed += 1
        session.flush()
        if canonical is None:
            canonical = SidecarRegistry(sidecar_id=norm, **merged)
            session.add(canonical)
        else:
            for k, v in merged.items():
                setattr(canonical, k, v)
    return removed


def migrate(apply: bool) -> int:
    with Session(engine) as session:
        mapping = _build_mapping(_distinct_sidecar_ids(session))
        if not mapping:
            print("No drifting sidecar ids — nothing to do.")
            return 0

        print("Planned folds (src -> normalized):")
        affected_providers: set[str] = set()
        for src, tgt in sorted(mapping.items()):
            n_events = len(
                session.exec(select(UsageEvent.id).where(UsageEvent.sidecar_id == src)).all()
            )
            n_latest = len(
                session.exec(select(LatestUsage.id).where(LatestUsage.sidecar_id == src)).all()
            )
            n_win = len(
                session.exec(select(UsageWindow.id).where(UsageWindow.sidecar_id == src)).all()
            )
            for p in session.exec(
                select(UsageEvent.provider_id).where(UsageEvent.sidecar_id == src).distinct()
            ).all():
                affected_providers.add(p)
            print(
                f"  {src!r} -> {tgt!r}: {n_events} events, {n_latest} latest_usage, {n_win} windows"
            )

        if not apply:
            print("\nDry run — no changes written. Re-run with --apply to execute.")
            return 0

        # 1. Retag events + live cards (sidecar_id excluded from both identity
        #    unique-constraints, so a plain UPDATE never collides).
        for src, tgt in mapping.items():
            session.exec(
                update(UsageEvent).where(UsageEvent.sidecar_id == src).values(sidecar_id=tgt)  # type: ignore[arg-type]
            )
            session.exec(
                update(LatestUsage).where(LatestUsage.sidecar_id == src).values(sidecar_id=tgt)  # type: ignore[arg-type]
            )
        # 2. Merge the closed-window archive (not event-derived) + the registry.
        folded = _merge_windows(session, mapping)
        removed = _merge_registry(session, mapping)
        session.commit()
        print(
            f"Retagged events/latest_usage; folded {folded} window row(s); removed {removed} registry row(s)."
        )

    # 3. Rebuild rollups from the now-retagged events, scoped to affected
    #    providers (opens its own session; must run after the commit above).
    if affected_providers:
        providers = sorted(affected_providers)
        print(f"Rebuilding rollups for affected providers: {', '.join(providers)}")
        n = backfill(providers)
        print(f"Rebuilt rollups from {n:,} event(s).")
    else:
        print("No events to rebuild rollups from.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Preview planned folds, write nothing.")
    g.add_argument("--apply", action="store_true", help="Execute the migration.")
    args = p.parse_args()
    return migrate(apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
