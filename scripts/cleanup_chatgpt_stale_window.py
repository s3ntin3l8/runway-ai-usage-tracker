#!/usr/bin/env python3
"""One-shot cleanup for stale ChatGPT/Codex gauge cards left behind by a
Codex plan change (e.g. Go -> Plus/Pro, or back).

Background
----------
The set of `window_type`s the collector reports for an account is a function
of the account's *current* plan: Go/free-tier payloads only ever send
`rate_limit.primary_window` with no duration field, which the collector maps
to `window_type: "monthly"`; Plus/Pro payloads send both `primary_window`
(5h `session`) and `secondary_window` (7d `weekly`) — see
app/services/collectors/chatgpt_web.py. None of these are inherently wrong;
each is the correct card set for the plan that produced it.

The problem is direction-agnostic: whichever window_type(s) an account's
*previous* plan reported become stale the moment the plan changes, and
nothing deletes them automatically. `upsert_latest_usage`'s cross-window_type
delete (accumulator.py) is guarded on `if model_id:`, and ChatGPT cards carry
no model_id (that guard exists so aggregate cards, like a provider that
legitimately keeps session+weekly forever, are never swept just because a
sibling window_type row exists). `prune_stale_latest_usage` only runs on a
sidecar push, and ChatGPT is server-collected. So the orphan(s) just sit
there next to the current plan's cards indefinitely.

This script deletes explicitly-named stale window_type row(s) for ONE named
account_id — never a blanket sweep of every chatgpt row of that window_type,
since that would also delete another account's legitimate card of the same
type in a multi-account deployment. latest_usage holds only live gauges — no
event/rollup/history data is lost either way.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1,
AFTER at least one successful poll under the new plan has produced fresh
card(s) for the account (otherwise you'd be deleting the only cards that
account has):

  # Go/free -> Plus/Pro: the old monthly card is now stale.
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_chatgpt_stale_window.py --account-id <id> \\
      --window-type monthly --dry-run
  # eyeball the row(s), then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_chatgpt_stale_window.py --account-id <id> \\
      --window-type monthly --apply

  # Plus/Pro -> Go/free: both old cards are now stale.
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/cleanup_chatgpt_stale_window.py --account-id <id> \\
      --window-type session weekly --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, delete, select  # noqa: E402

from app.core.console import force_utf8_console  # noqa: E402
from app.core.db import engine  # noqa: E402
from app.models.db import LatestUsage  # noqa: E402

_PROVIDER = "chatgpt"
# The only window_types the ChatGPT collector can ever emit (see
# _classify_window_seconds in chatgpt_web.py) — constraining --window-type
# to these turns a typo into an immediate argparse error instead of a silent
# 0-row no-op.
_VALID_WINDOW_TYPES = ("session", "daily", "weekly", "monthly")


def _filters(account_id: str, window_types: list[str]) -> list:
    return [
        LatestUsage.provider_id == _PROVIDER,
        LatestUsage.account_id == account_id,
        LatestUsage.window_type.in_(window_types),  # type: ignore[attr-defined]
        # ChatGPT cards are never model-scoped; pin this explicitly so the
        # script can never touch a hypothetical per-model row.
        LatestUsage.model_id == "",
    ]


def main() -> int:
    force_utf8_console()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--account-id",
        required=True,
        help="The account_id whose plan changed (see latest_usage.account_id). "
        "Scoping is deliberate — another account's card of the same window_type "
        "is correct and must not be swept up alongside a switched account's stale one.",
    )
    p.add_argument(
        "--window-type",
        required=True,
        nargs="+",
        choices=_VALID_WINDOW_TYPES,
        help="One or more stale window_type(s) to delete, e.g. `monthly` after a "
        "Go->Plus upgrade, or `session weekly` after a Plus->Go downgrade.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted without writing."
    )
    g.add_argument("--apply", action="store_true", help="Delete the stale row(s).")
    args = p.parse_args()
    dry_run = args.dry_run
    prefix = "[DRY-RUN] " if dry_run else ""

    window_types_label = "+".join(args.window_type)

    with Session(engine) as session:
        rows = session.exec(
            select(LatestUsage).where(*_filters(args.account_id, args.window_type))
        ).all()
        verb = "Would delete" if dry_run else "Deleting"
        print(
            f"{prefix}{verb} {len(rows)} latest_usage row(s) "
            f"({_PROVIDER}/{window_types_label}/{args.account_id!r}):",
            flush=True,
        )
        for r in rows:
            print(
                f"  id={r.id} account_id={r.account_id!r} window_type={r.window_type!r} "
                f"model_id={r.model_id!r} updated_at={r.updated_at}",
                flush=True,
            )

        if not dry_run and rows:
            session.exec(delete(LatestUsage).where(*_filters(args.account_id, args.window_type)))
            session.commit()
            print(f"Deleted {len(rows)} row(s).", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
