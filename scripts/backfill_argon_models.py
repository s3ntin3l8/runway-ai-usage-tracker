#!/usr/bin/env python3
"""Repair already-ingested Gemini 4 Argon usage_events rows — issue #686.

Background
----------
The Antigravity extractor reported the raw model id as "gemini-4-argon-trial"
(the "-trial" suffix is part of the id, not a version qualifier) and the
Gemini/Hermes normalizer passed it through verbatim, so usage_events rows
carry "gemini-4-argon-trial" while the seeded pricing rows are keyed on the
canonical "gemini-4-argon" (see app/services/pricing_seed.py). An event
whose id matches no pricing row is priced at $0.00.

Both normalizers now canonicalise the Argon family (see
app/services/model_normalization.py), so newly ingested events store
"gemini-4-argon". This script repairs the rows that were ingested before that:
it rewrites the stored model_id and re-prices each affected event from
provider_pricing with the same rule ingest uses, so the $0.00 rows pick up the
Argon rate. The rollups and windows of every touched (provider, account) pair
are rebuilt afterwards, because model_id is part of the rollup grain and of the
window series identity.

This is a thin CLI wrapper — the logic lives in
app/services/maintenance/argon_retag.py, shared with the app's own cost rules
(app/services/maintenance/event_cost.py) so a host-run script and an in-app
fix can never drift apart.

Run with the server STOPPED (SQLite is single-writer) and point
RUNWAY_CONFIG_DIR at the target DB:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \
      python scripts/backfill_argon_models.py --dry-run
  # eyeball the planned changes, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \
      python scripts/backfill_argon_models.py

Verify after apply:

  # every Argon event now carries the canonical id
  sqlite3 "$RUNWAY_CONFIG_DIR/runway.db" \
    "SELECT provider_id, model_id, COUNT(*), ROUND(SUM(cost_usd), 4) \
     FROM usage_events WHERE model_id LIKE 'gemini-4-argon%' \
     GROUP BY provider_id, model_id;"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.services.maintenance.argon_retag import (  # noqa: E402
    ARGON_PROVIDER_IDS,
    apply_argon_retag,
    plan_argon_retag,
)
from app.services.model_normalization import ARGON_MODEL_ID  # noqa: E402


def run(providers: list[str] | None, dry_run: bool, skip_rollups: bool, skip_windows: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    scope = ", ".join(providers) if providers else ", ".join(ARGON_PROVIDER_IDS)
    print(f"{prefix}Repairing Gemini 4 Argon model ids ({scope})…", flush=True)

    with Session(engine) as session:
        if dry_run:
            plan = plan_argon_retag(session, providers, sample_size=10)
            if not plan.total:
                print("  no Argon model ids to repair — nothing to do.")
                return
            for model_id, count in sorted(plan.by_model.items()):
                print(f"  {model_id} -> {ARGON_MODEL_ID}: {count} event(s)")
            print(
                f"  would rewrite {plan.total} event(s); "
                f"{plan.repriced} re-priced, {plan.unpriced} still unpriced; "
                f"cost delta ${plan.cost_delta_usd:,.4f}."
            )
            for sample in plan.samples:
                print(
                    f"    {sample.event_ref} — {sample.old_model_id} "
                    f"${sample.old_cost_usd:.4f} -> ${sample.resolved.cost_usd:.4f}"
                )
            return

        result = apply_argon_retag(
            session, providers, skip_rollups=skip_rollups, skip_windows=skip_windows
        )
        print(
            f"  rewrote {result.retagged} event(s); {result.repriced} re-priced, "
            f"{result.unpriced} still unpriced; cost delta ${result.cost_delta_usd:,.4f}."
        )
        if not skip_rollups:
            print(
                f"  rollups rebuilt for {result.rollups_rebuilt_pairs} (provider, account) pair(s)."
            )
        else:
            print("  rollup rebuild skipped (--skip-rollups).")
        if not skip_windows:
            print(f"  windows rebuilt: {result.windows_rebuilt}.")
        else:
            print("  window rebuild skipped (--skip-windows).")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--provider",
        action="append",
        default=[],
        metavar="ID",
        help="Provider id to repair (repeatable). Defaults to "
        + ", ".join(ARGON_PROVIDER_IDS)
        + ".",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    p.add_argument("--skip-rollups", action="store_true", help="Skip the rollup rebuild.")
    p.add_argument("--skip-windows", action="store_true", help="Skip the window rebuild.")
    args = p.parse_args(argv)
    run(
        providers=args.provider or None,
        dry_run=args.dry_run,
        skip_rollups=args.skip_rollups,
        skip_windows=args.skip_windows,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
