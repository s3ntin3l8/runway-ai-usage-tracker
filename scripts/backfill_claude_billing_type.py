#!/usr/bin/env python3
"""Price the Claude accounts that were created before billing detection existed.

A detected billing model only ever lands on a **new** account row, so every
Claude account created before the detection existed keeps
``billing_type = "unknown"`` and the dashboard shows a usage value where a
plan shows an estimate.

The evidence is the account's own published cards: no credential is decrypted
and no provider is called. An account whose model an operator set by hand is
never touched.

The same repair runs from Settings -> Data health; both call the repair in
`app/services/maintenance/claude_billing_type.py`, so they cannot drift apart.

Run against the dev DB or a snapshot:

  APP_HOST=127.0.0.1 python scripts/backfill_claude_billing_type.py
  APP_HOST=127.0.0.1 python scripts/backfill_claude_billing_type.py --apply
  APP_HOST=127.0.0.1 python scripts/backfill_claude_billing_type.py --provider anthropic
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from app.core.console import force_utf8_console  # noqa: E402
from app.core.db import engine, init_db  # noqa: E402
from app.services.maintenance.claude_billing_type import (  # noqa: E402
    apply_claude_billing_type,
    plan_claude_billing_type,
)


def run(provider_id: str | None, apply_changes: bool) -> None:
    prefix = "" if apply_changes else "[DRY-RUN] "
    # Idempotently ensure the billing_type_source column exists (ALTER ADD
    # COLUMN) so this works even against a DB the server has not started
    # against yet.
    init_db()

    with Session(engine) as session:
        plan = plan_claude_billing_type(session, provider_id=provider_id)
        if not plan.total:
            print(f"{prefix}No Claude account is waiting for a billing model.")
            return
        print(
            f"{prefix}{plan.total} Claude account(s) have no billing model: "
            f"{len(plan.priced)} can be priced, {len(plan.skipped)} have no "
            "published card that decides one.",
            flush=True,
        )
        for candidate in plan.priced:
            print(
                f"  {candidate.provider_id}/{candidate.account_id} -> {candidate.model}",
                flush=True,
            )
        for candidate in plan.skipped:
            print(
                f"  {candidate.provider_id}/{candidate.account_id} -> skipped "
                "(no published card decides a billing model)",
                flush=True,
            )
        if not apply_changes:
            print("Dry run — nothing was written. Re-run with --apply to price them.")
            return
        result = apply_claude_billing_type(session, provider_id=provider_id)
        print(f"Priced {result.priced} account(s); {result.skipped} left alone.", flush=True)


def main() -> int:
    force_utf8_console()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--provider",
        default=None,
        metavar="ID",
        help="Only price this provider's accounts (default: every Claude provider).",
    )
    p.add_argument(
        "--apply", action="store_true", help="Write the detected models (default: preview only)."
    )
    args = p.parse_args()
    run(provider_id=args.provider, apply_changes=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
