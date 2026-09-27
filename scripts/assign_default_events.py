#!/usr/bin/env python3
"""Move explicitly selected legacy ``default`` events to a configured account.

This is the manual repair path for data collected before unresolved events were
held in ``pending_usage_events``. It shows the proposed event list by default;
pass ``--apply`` only after reviewing that list.

This is a thin CLI wrapper — the actual logic lives in
app/services/maintenance/event_reassign.py, shared with the in-app Data
Health `lone_default_events` fixer so a host-run script and an in-app fix
can never drift apart.

Example:
  python scripts/assign_default_events.py --provider minimax \\
      --account-id alice@example.com --event-id msg-123 --dry-run
  python scripts/assign_default_events.py --provider minimax \\
      --account-id alice@example.com --event-id msg-123 --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.core.db import engine, init_db  # noqa: E402
from app.models.db import ProviderConfig  # noqa: E402
from app.services.maintenance.event_reassign import (  # noqa: E402
    apply_reassign_default,
    plan_reassign_default,
)

_SOURCE_ACCOUNT_ID = "default"


def assign_events(provider_id: str, account_id: str, event_ids: list[str], apply: bool) -> int:
    if not event_ids:
        raise ValueError("provide one or more --event-id values")
    init_db()
    with Session(engine) as session:
        config = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == provider_id,
                ProviderConfig.account_id == account_id,
            )
        ).first()
        if config is None:
            raise ValueError(f"No configured account {provider_id}/{account_id}")

        try:
            plan = plan_reassign_default(
                session,
                provider_id=provider_id,
                source=_SOURCE_ACCOUNT_ID,
                target=account_id,
                event_ids=event_ids,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        for event in plan.samples:
            print(
                f"{event.event_id} {event.ts.isoformat()} sidecar={event.sidecar_id} "
                f"model={event.model_id or '?'} tokens="
                f"{event.tokens_input + event.tokens_output + event.tokens_cache_read + event.tokens_cache_create} "
                f"value=${event.cost_usd:.6f}"
            )
        print(
            f"{plan.count} event(s): {provider_id}/{_SOURCE_ACCOUNT_ID} → {provider_id}/{account_id}"
        )
        if not apply:
            print("Dry run only. Re-run with --apply to move events and rebuild affected totals.")
            return plan.count

        result = apply_reassign_default(
            session,
            provider_id=provider_id,
            source=_SOURCE_ACCOUNT_ID,
            target=account_id,
            event_ids=event_ids,
        )
        print("Assignment applied; period rollups and affected closed windows updated.")
        return result.moved


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--provider", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument(
        "--event-id",
        action="append",
        dest="event_ids",
        default=[],
        help="Event id to move (repeatable).",
    )
    p.add_argument(
        "--apply", action="store_true", help="Actually move events (default: preview only)."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="No-op; preview is already the default without --apply.",
    )
    args = p.parse_args()

    try:
        assign_events(args.provider, args.account_id, args.event_ids, apply=args.apply)
    except ValueError as exc:
        p.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
