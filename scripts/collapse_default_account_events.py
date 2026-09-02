#!/usr/bin/env python3
"""Collapse duplicated usage_events rows left over from a stale account_id.

Background
----------
Several providers (antigravity, opencode, opencode-free, chatgpt) used to
resolve account identity to "default" before the sidecar was fixed to emit
the real account email (the same class of bug scripts/merge_gemini_default_
account.py already fixed for Gemini's *quota cards*) — but for these
providers the duplication also hit the raw usage_events ledger itself, not
just the derived latest_usage gauge. Because usage_events is keyed on
(provider_id, account_id, event_id), a change in account_id resolution
doesn't get deduped by the unique constraint: the exact same message can end
up ingested twice, once under "default" and once under the real email,
double-counting tokens/cost/messages in every downstream total (cumulative
stats, top-models, forecasts) for that provider.

Verified against production data before writing this script (see PR
description for the full breakdown):
  - Every duplicate group has EXACTLY 2 rows (one "default", one real
    identity) — never 3+.
  - Most pairs are byte-identical duplicates. A minority genuinely diverge:
    * tokens differ (antigravity: never — ts differs by seconds-to-hours
      instead, tokens always match exactly; opencode: 754 pairs;
      opencode-free: 442 pairs) — when they do, tokens_input and
      tokens_output always agree on which side is larger (0 counter-examples
      checked), consistent with OpenCode re-syncing a still-streaming
      message at two different poll times.
    * kind differs (opencode: 9 pairs, opencode-free: 10) — always the
      "default" copy is kind="error" (zeroed) and the real-identity copy is
      kind="message" with real data.

Given that, the winner of a duplicate pair is picked by, in order: (1)
kind="message" beats kind="error", (2) larger tokens_input+tokens_output,
(3) lower id (first-ingested) as a final deterministic tie-break for true
duplicates. The loser row is deleted outright — rows are never merged
field-by-field, since the winner is already a complete, self-consistent
record. The survivor's account_id is then retagged to the canonical target
identity if it isn't already (safe: the loser vacated that (provider_id,
account_id, event_id) slot first). Non-duplicated ("lone") events still
sitting under "default" are retagged to the target too, for full identity
consistency — collision-free by construction, since "lone" means no other
row shares that event_id under this provider at all.

usage_period_rollup is rebuilt afterward (scripts/backfill_rollups.py).

Known limitation — NOT handled by this script: usage_windows is a frozen
archive ("written exactly once ... never updated", app/models/db.py). Any
already-closed window whose totals were inflated by a duplicate before this
migration runs stays inflated; only windows that close *after* the fix will
be correct. Retroactively recomputing closed windows is a separate, harder
problem (would need to replay window_closer historically) and is out of
scope here.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/collapse_default_account_events.py --provider antigravity --dry-run
  # eyeball the plan, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
    python scripts/collapse_default_account_events.py --provider antigravity --apply

Repeat per provider (antigravity, opencode, opencode-free, chatgpt, or any
other provider where the same "default"-vs-real-identity split shows up) —
--provider is required and not defaulted, since blindly scanning every
provider in the table risks acting on an identity split that has a
legitimate reason to exist (e.g. two genuinely different real accounts).
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
from app.models.db import UsageEvent  # noqa: E402
from scripts.backfill_rollups import backfill  # noqa: E402

_SOURCE_ACCOUNT_ID = "default"


def _resolve_target(session: Session, provider_id: str, source: str) -> str | None:
    """The single non-source account_id present for this provider, or None."""
    accounts = {
        a
        for a in session.exec(
            select(UsageEvent.account_id).where(UsageEvent.provider_id == provider_id).distinct()
        ).all()
        if a and a != source
    }
    if len(accounts) == 1:
        return next(iter(accounts))
    return None


def _pick_winner(a: UsageEvent, b: UsageEvent) -> tuple[UsageEvent, UsageEvent]:
    """Return (winner, loser). See module docstring for the ordering rationale."""
    a_is_message = a.kind == "message"
    b_is_message = b.kind == "message"
    if a_is_message != b_is_message:
        return (a, b) if a_is_message else (b, a)

    a_tokens = a.tokens_input + a.tokens_output
    b_tokens = b.tokens_input + b.tokens_output
    if a_tokens != b_tokens:
        return (a, b) if a_tokens > b_tokens else (b, a)

    return (a, b) if (a.id or 0) < (b.id or 0) else (b, a)


def migrate(provider_id: str, source: str, target: str | None, apply: bool) -> dict[str, int]:
    stats = {"pairs": 0, "lone_retagged": 0, "deleted": 0, "retagged": 0}

    with Session(engine) as session:
        resolved = target or _resolve_target(session, provider_id, source)
        if not resolved:
            print(
                f"Could not resolve a unique target account_id for provider {provider_id!r} "
                f"(pass --account-id). Aborting."
            )
            return stats
        print(f"Collapsing {provider_id!r} account {source!r} -> {resolved!r}\n")

        rows = session.exec(select(UsageEvent).where(UsageEvent.provider_id == provider_id)).all()
        by_event: dict[str, list[UsageEvent]] = {}
        for r in rows:
            by_event.setdefault(r.event_id, []).append(r)

        for event_id, group in sorted(by_event.items()):
            accounts = {r.account_id for r in group}
            if len(accounts) == 1:
                if accounts == {source}:
                    # Lone "default" row, no duplicate — just retag.
                    row = group[0]
                    print(f"  retag (lone): {event_id} {source!r} -> {resolved!r}")
                    stats["lone_retagged"] += 1
                    if apply:
                        row.account_id = resolved
                continue

            if len(group) != 2 or accounts != {source, resolved}:
                print(
                    f"  ! {event_id}: unexpected account spread {sorted(accounts)} "
                    f"({len(group)} row(s)) — skipping, needs manual review"
                )
                continue

            winner, loser = _pick_winner(group[0], group[1])
            stats["pairs"] += 1
            note = "" if winner.kind == loser.kind else f" (kind: {loser.kind}->{winner.kind})"
            note += "" if winner.ts == loser.ts else f" (ts differs: {loser.ts} vs {winner.ts})"
            print(
                f"  {event_id}: keep {winner.account_id!r}/{winner.kind}"
                f" (tokens {winner.tokens_input}+{winner.tokens_output}), "
                f"drop {loser.account_id!r}{note}"
            )
            if apply:
                session.delete(loser)
                session.flush()
                stats["deleted"] += 1
                if winner.account_id != resolved:
                    winner.account_id = resolved
                    stats["retagged"] += 1

        if apply:
            session.commit()

    print(
        f"\n{'' if apply else '[DRY-RUN] '}{stats['pairs']:,} duplicate pair(s), "
        f"{stats['lone_retagged']:,} lone 'default' row(s) retagged, "
        f"{stats['deleted']:,} row(s) deleted, {stats['retagged']:,} winner(s) retagged.",
        flush=True,
    )

    if apply and (stats["pairs"] or stats["lone_retagged"]):
        print(f"Rebuilding rollups for: {provider_id}", flush=True)
        backfill([provider_id])

    return stats


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--provider", required=True, help="Provider id to collapse (required).")
    p.add_argument(
        "--source", default=_SOURCE_ACCOUNT_ID, help="Stale account id (default: 'default')."
    )
    p.add_argument(
        "--account-id",
        default=None,
        help="Target canonical account_id. Auto-detected if exactly one non-source "
        "account exists for this provider.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Preview the plan, write nothing.")
    g.add_argument("--apply", action="store_true", help="Execute the migration.")
    args = p.parse_args()
    migrate(args.provider, args.source, args.account_id, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
