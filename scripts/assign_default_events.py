#!/usr/bin/env python3
"""Move explicitly selected legacy ``default`` events to a configured account.

This is the manual repair path for data collected before unresolved events were
held in ``pending_usage_events``. It shows the proposed event list by default;
pass ``--apply`` only after reviewing that list.

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
from app.models.db import ProviderConfig, UsageEvent, UsageWindow  # noqa: E402
from app.services.period_rollups import update_rollups_for_event  # noqa: E402
from app.services.window_closer import close_window  # noqa: E402


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

        events = list(
            session.exec(
                select(UsageEvent).where(
                    UsageEvent.provider_id == provider_id,
                    UsageEvent.event_id.in_(event_ids),
                    UsageEvent.account_id == "default",
                )
            ).all()
        )
        if len(events) != len(set(event_ids)):
            raise ValueError("Every selected event must exist under the provider's default account")

        for event in events:
            print(
                f"{event.event_id} {event.ts.isoformat()} sidecar={event.sidecar_id} "
                f"model={event.model_id or '?'} tokens="
                f"{event.tokens_input + event.tokens_output + event.tokens_cache_read + event.tokens_cache_create} "
                f"value=${event.cost_usd:.6f}"
            )
        print(f"{len(events)} event(s): {provider_id}/default → {provider_id}/{account_id}")
        if not apply:
            print("Dry run only. Re-run with --apply to move events and rebuild affected totals.")
            return len(events)

        boundaries: dict[tuple[str, str, object, object], tuple[float | None, float | None]] = {}
        for event in events:
            rows = session.exec(
                select(UsageWindow).where(
                    UsageWindow.provider_id == provider_id,
                    UsageWindow.account_id.in_(["default", account_id]),
                    UsageWindow.window_start <= event.ts,
                    UsageWindow.window_end > event.ts,
                )
            ).all()
            for row in rows:
                for aid in ("default", account_id):
                    key = (aid, row.window_type, row.window_start, row.window_end)
                    boundaries[key] = (row.limit_value, row.pct_used)

        for event in events:
            if event.kind == "message":
                update_rollups_for_event(session, event, sign=-1)
            event.account_id = account_id
            event.attribution_source = "tag"
            session.add(event)
            if event.kind == "message":
                update_rollups_for_event(session, event)

        for row in session.exec(
            select(UsageWindow).where(
                UsageWindow.provider_id == provider_id,
                UsageWindow.account_id.in_(["default", account_id]),
                UsageWindow.window_start <= max(event.ts for event in events),
                UsageWindow.window_end > min(event.ts for event in events),
            )
        ).all():
            key = (row.account_id, row.window_type, row.window_start, row.window_end)
            if key in boundaries:
                session.delete(row)
        session.flush()
        for (aid, window_type, start, end), (limit_value, pct_used) in boundaries.items():
            close_window(
                session,
                provider_id=provider_id,
                account_id=aid,
                window_type=window_type,
                window_start=start,  # type: ignore[arg-type]
                window_end=end,  # type: ignore[arg-type]
                limit_value=limit_value,
                pct_used=pct_used,
            )
        session.commit()

        print("Assignment applied; period rollups and affected closed windows updated.")
        return len(events)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--event-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed assignment.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Explicitly select preview mode (the default)."
    )
    args = parser.parse_args()
    try:
        assign_events(args.provider, args.account_id, args.event_id, args.apply)
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
