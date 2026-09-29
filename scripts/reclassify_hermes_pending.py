#!/usr/bin/env python3
"""Reclassify Hermes pending usage events (and legacy usage_events) to canonical providers.

Background
----------
Hermes Agent session usage previously pushed events under synthetic or
unrecognized provider IDs:
  - 'hermes-xai-oauth' -> should be 'xai'
  - 'hermes-auto' -> 'opencode-free' (for free models) or 'opencode' (for subscription models)
  - 'hermes' -> 'kimi_coding' (for kimi-for-coding / kimi-* models),
                'opencode-free' (for free models),
                or 'opencode' (for subscription models)

This script scans pending_usage_events (and any usage_events) tagged with
these legacy IDs, determines the canonical provider from the event model and task,
and updates the database rows in place.

Note on rollups:
  When --apply is passed, database updates to usage_events are committed first,
  followed by a best-effort rollup rebuild across affected providers. If the rollup
  rebuild encounters an issue, the table rewrite remains committed and rollups can be
  rebuilt manually via `scripts.backfill_rollups.backfill(provider_id)`.

Usage:
  # Dry run preview (default):
  RUNWAY_CONFIG_DIR=~/.config/runway python scripts/reclassify_hermes_pending.py

  # Apply changes:
  RUNWAY_CONFIG_DIR=~/.config/runway python scripts/reclassify_hermes_pending.py --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, col, select  # noqa: E402

from app.core.db import engine, init_db  # noqa: E402
from app.models.db import PendingUsageEvent, UsageEvent  # noqa: E402
from scripts.sidecar_pkg.event_extractors.hermes import _is_free_model  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("reclassify_hermes")

LEGACY_PROVIDERS = ("hermes", "hermes-auto", "hermes-xai-oauth")


def determine_canonical_provider(legacy_provider: str, model: str) -> str:
    """Return the canonical provider_id for a legacy Hermes provider and model."""
    m = (model or "").strip()
    m_lower = m.lower()

    if legacy_provider == "hermes-xai-oauth":
        return "xai"
    if legacy_provider == "hermes-auto":
        return "opencode-free" if _is_free_model(m) else "opencode"
    if legacy_provider == "hermes":
        if m_lower.startswith("kimi-") or m_lower == "kimi-for-coding":
            return "kimi_coding"
        if m_lower.startswith("grok-"):
            return "xai"
        if m_lower.startswith("minimax") or m_lower.startswith("minimax-"):
            return "minimax"
        if _is_free_model(m):
            return "opencode-free"
        return "opencode"
    return legacy_provider


def reclassify_pending(session: Session, dry_run: bool) -> int:
    """Reclassify pending_usage_events under legacy Hermes provider IDs."""
    rows = session.exec(
        select(PendingUsageEvent).where(col(PendingUsageEvent.provider_id).in_(LEGACY_PROVIDERS))
    ).all()
    if not rows:
        logger.info("No pending events found with legacy Hermes provider IDs.")
        return 0

    reclassified = 0
    counts_by_target: dict[str, int] = {}
    skipped_by_target: dict[str, int] = {}
    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            payload = {}
        model = payload.get("model_id") or ""
        target_provider = determine_canonical_provider(row.provider_id, model)
        if target_provider == row.provider_id:
            continue

        # Check collision against unique constraint (provider_id, event_id, sidecar_id)
        existing = session.exec(
            select(PendingUsageEvent).where(
                PendingUsageEvent.provider_id == target_provider,
                PendingUsageEvent.event_id == row.event_id,
                PendingUsageEvent.sidecar_id == row.sidecar_id,
            )
        ).first()
        if existing is not None:
            skipped_by_target[target_provider] = skipped_by_target.get(target_provider, 0) + 1
            logger.warning(
                "Skipping collision: pending event %s (%s) already exists under %s",
                row.event_id,
                row.provider_id,
                target_provider,
            )
            continue

        if not dry_run:
            payload["provider_id"] = target_provider
            row.provider_id = target_provider
            row.payload_json = json.dumps(payload)
            session.add(row)

        counts_by_target[target_provider] = counts_by_target.get(target_provider, 0) + 1
        reclassified += 1

    for target_provider in sorted(set(counts_by_target) | set(skipped_by_target)):
        count = counts_by_target.get(target_provider, 0)
        skipped = skipped_by_target.get(target_provider, 0)
        msg = f"  -> {target_provider}: {count} event(s)"
        if skipped > 0:
            msg += f" ({skipped} skipped due to collision)"
        logger.info(msg)

    if not dry_run and reclassified > 0:
        session.commit()
        logger.info("Committed %d reclassified pending usage event(s).", reclassified)
    return reclassified


def reclassify_usage_events(session: Session, dry_run: bool) -> int:
    """Reclassify usage_events rows under legacy Hermes provider IDs."""
    rows = session.exec(
        select(UsageEvent).where(col(UsageEvent.provider_id).in_(LEGACY_PROVIDERS))
    ).all()
    if not rows:
        return 0

    reclassified = 0
    counts_by_target: dict[str, int] = {}
    skipped_by_target: dict[str, int] = {}
    affected_providers: set[str] = set()
    for row in rows:
        target_provider = determine_canonical_provider(row.provider_id, row.model_id or "")
        if target_provider == row.provider_id:
            continue
        # Matches unique index uq_usage_events_provider_event on (provider_id, event_id)
        existing = session.exec(
            select(UsageEvent).where(
                UsageEvent.provider_id == target_provider,
                UsageEvent.event_id == row.event_id,
            )
        ).first()
        if existing is not None:
            skipped_by_target[target_provider] = skipped_by_target.get(target_provider, 0) + 1
            logger.warning(
                "Skipping collision: UsageEvent %s (%s) already exists under %s",
                row.event_id,
                row.provider_id,
                target_provider,
            )
            continue

        if not dry_run:
            affected_providers.add(row.provider_id)
            affected_providers.add(target_provider)
            row.provider_id = target_provider
            session.add(row)
        counts_by_target[target_provider] = counts_by_target.get(target_provider, 0) + 1
        reclassified += 1

    for target_provider in sorted(set(counts_by_target) | set(skipped_by_target)):
        count = counts_by_target.get(target_provider, 0)
        skipped = skipped_by_target.get(target_provider, 0)
        msg = f"  -> {target_provider}: {count} event(s)"
        if skipped > 0:
            msg += f" ({skipped} skipped due to collision)"
        logger.info(msg)

    if not dry_run and reclassified > 0:
        session.commit()
        logger.info("Committed %d reclassified usage_events row(s).", reclassified)
        try:
            from scripts.backfill_rollups import backfill

            for p in sorted(affected_providers):
                backfill(p)
        except Exception as e:
            logger.warning("Failed to rebuild rollups: %s", e)
    return reclassified


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply reclassification to the database (default is dry-run)",
    )
    args = parser.parse_args()

    init_db()
    dry_run = not args.apply
    if dry_run:
        logger.info("--- DRY RUN (pass --apply to execute changes) ---")
    else:
        logger.info("--- APPLYING CHANGES ---")

    with Session(engine) as session:
        pending_count = reclassify_pending(session, dry_run=dry_run)
        events_count = reclassify_usage_events(session, dry_run=dry_run)

    logger.info(
        "Done. Total reclassified: %d pending event(s), %d usage_event(s).",
        pending_count,
        events_count,
    )


if __name__ == "__main__":
    main()
