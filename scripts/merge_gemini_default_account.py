#!/usr/bin/env python3
"""Collapse a duplicated Gemini account identity (``default`` -> email).

Background
----------
While Gemini quota was collected server-side in local mode, the server read
``~/.gemini/oauth_creds.json`` directly — ``id_token`` included — and resolved the
canonical email ``account_id`` (see ``app/services/account_identity.py``). When
collection moved to the sidecar-only path, the sidecar's credential mapping
omitted the ``id_token`` (fixed in ``scripts/sidecar.py`` / ``app/core/registry.json``),
so the server could no longer derive the email and fell back to
``account_id="default"``. That spawned a second set of quota cards (one per model)
which the dashboard — grouping by ``(provider_id, account_id)`` — renders as a
duplicate Gemini card.

The mapping fix stops this going forward. This one-shot re-keys the already-orphaned
``default`` rows onto the canonical email so the live quota re-merges with the
email-keyed event enrichment into a single card. ``usage_events`` /
``usage_period_rollup`` are already email-keyed and untouched.

This is a thin CLI wrapper — the actual logic lives in
app/services/maintenance/account_merge.py, shared with the in-app Data
Health `orphan_gauge_series` fixer so a host-run script and an in-app fix
can never drift apart.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1::

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/merge_gemini_default_account.py --dry-run
  # eyeball the plan, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/merge_gemini_default_account.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root is on sys.path when the script is invoked directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import LatestUsage  # noqa: E402
from app.services.maintenance.account_merge import (  # noqa: E402
    merge_gauge_series,
    plan_merge_gauge_series,
)


def _resolve_target_email(session: Session, provider_id: str, source: str) -> str | None:
    """The single non-source account_id present for this provider, or None."""
    accounts = {
        a
        for a in session.exec(
            select(LatestUsage.account_id).where(LatestUsage.provider_id == provider_id).distinct()
        ).all()
        if a and a != source
    }
    if len(accounts) == 1:
        return next(iter(accounts))
    return None


def migrate(provider_id: str, source: str, target: str | None, apply: bool) -> int:
    with Session(engine) as session:
        resolved = target or _resolve_target_email(session, provider_id, source)
        if not resolved:
            print(
                f"Could not resolve a unique target email for provider {provider_id!r} "
                f"(pass --email). Aborting."
            )
            return 1
        print(f"Folding {provider_id!r} account {source!r} -> {resolved!r}\n")

        if not apply:
            plan = plan_merge_gauge_series(
                session, provider_id=provider_id, source=source, target=resolved
            )
            print(f"latest_usage: {plan.merged} merge, {plan.retagged} retag (no matching card)")
            for line in plan.samples:
                print(f"  {line}")
            print(
                f"quota_snapshots: {plan.snapshots_retagged} retag -> {resolved!r}, "
                f"{plan.snapshots_collided} exact-ts duplicate(s) would be dropped"
            )
            print("\nDry run — no changes written. Re-run with --apply to execute.")
            return 0

        result = merge_gauge_series(
            session, provider_id=provider_id, source=source, target=resolved
        )
        print(f"latest_usage: {result.merged} merged, {result.retagged} retagged")
        print(
            f"quota_snapshots: {result.snapshots_retagged} retagged, "
            f"{result.snapshots_collided} exact-ts duplicate(s) dropped"
        )
        print("\nApplied. The live quota now resolves under the canonical email account.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--provider", default="gemini", help="Provider id (default: gemini).")
    p.add_argument("--source", default="default", help="Account id to fold (default: default).")
    p.add_argument(
        "--email",
        default=None,
        help="Target canonical account_id. Auto-detected if exactly one non-source account exists.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Preview the plan, write nothing.")
    g.add_argument("--apply", action="store_true", help="Execute the migration.")
    args = p.parse_args()
    return migrate(args.provider, args.source, args.email, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
