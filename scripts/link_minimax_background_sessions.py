#!/usr/bin/env python3
"""File the MiniMax Code background-task rows under the session that spawned them.

The MiniMax Code extractor now files a background task's rows under the
**parent** session and tags them ``subagent_type="Explore"`` — the shape a
Claude Code sidechain row has. Rows collected before that change still carry
the background task's own session id, so they show up in the Sessions view as
a conversation of their own.

This script builds the mapping from the MiniMax Code runtime stores
(read-only) and applies it to the rows the server already holds. It only ever
**updates** the attribution: no row is inserted, no token count changes and no
rollup moves. A project's session count does drop — a background task was never
a conversation.

More than one store can exist on a host (a profile-scoped install keeps its
ledger next to the default one), and each holds its own background tasks, so
every store is repaired. Pass ``--store`` to repair specific ones instead.

The event ids are built with the extractor's own helper, so they cannot drift
from the ids the sidecar pushes.

A row that is still parked in ``pending_usage_events`` (an identity-less push
waiting for its account) is outside the repair: the promotion writes the
payload the sidecar sent, so re-run the repair after the operator assigns the
account. The run is idempotent, so that is enough.

Run against the dev DB or a snapshot — never the live prod DB while the
server is writing (SQLite is single-writer):

  python scripts/link_minimax_background_sessions.py --dry-run
  python scripts/link_minimax_background_sessions.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from app.core.console import force_utf8_console  # noqa: E402
from app.core.db import engine, init_db  # noqa: E402
from app.services.maintenance.minimax_background_sessions import (  # noqa: E402
    BackgroundLink,
    apply_background_attribution,
    plan_background_attribution,
)
from scripts.sidecar_pkg.event_extractors.minimax_code import (  # noqa: E402
    INTERNAL_TURN_PREFIX,
    SUBAGENT_TYPE_PLAN,
    background_provenance,
    background_session_links,
    build_event_id,
    discover_runtime_stores,
)


def _ledger_rows(cur: sqlite3.Cursor, session_ids: list[str]) -> list[sqlite3.Row]:
    """Return the ledger rows of the given sessions (read-only)."""
    if not session_ids:
        return []
    placeholders = ",".join("?" for _ in session_ids)
    query = (
        "SELECT session_id, turn_id, ts, input_tokens, output_tokens, cache_read_tokens "
        f"FROM local_runtime_token_usage WHERE session_id IN ({placeholders})"
    )
    cur.row_factory = sqlite3.Row
    return list(cur.execute(query, list(session_ids)).fetchall())


def collect_links(db_path: Path) -> list[BackgroundLink]:
    """Return the links the store says the collected rows should carry.

    A background task whose parent session is not in the store is still a
    background task: its rows keep their own session id and are only tagged,
    exactly as the extractor treats them. Such a task is **not** dropped —
    dropping it would leave the rows as untagged main-thread traffic.
    """
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        links_map = background_session_links(cur)
        links: list[BackgroundLink] = []
        for row in _ledger_rows(cur, list(links_map)):
            session_id = row["session_id"]
            link = links_map.get(session_id)
            if link is None:
                continue
            tokens_input = int(row["input_tokens"] or 0)
            tokens_output = int(row["output_tokens"] or 0)
            cache_read = int(row["cache_read_tokens"] or 0)
            event_id = build_event_id(
                session_id,
                row["turn_id"] or "",
                int(row["ts"] or 0),
                tokens_input,
                tokens_output,
                cache_read,
            )
            parent_id = link["parent_session_id"]
            turn_id = row["turn_id"] or ""
            # The label follows the row, not the session: the runtime's own
            # plan-review turn is a different kind of subagent work inside the
            # same session, and the extractor gives it the "Plan" label. The
            # repair has to agree, or it would flatten that label.
            if turn_id.startswith(INTERNAL_TURN_PREFIX):
                label = SUBAGENT_TYPE_PLAN
            else:
                label = str(link["label"])
            links.append(
                BackgroundLink(
                    event_id=event_id,
                    parent_session_id=parent_id,
                    subagent_type=label,
                    turn_id=row["turn_id"] or None,
                    raw_json=background_provenance(
                        session_id,
                        link["agent_name"],
                        parent_id,
                        link["purpose"],
                    ),
                )
            )
        return links
    finally:
        conn.close()


def main() -> None:
    force_utf8_console()
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="write the attribution",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing (the default)",
    )
    parser.add_argument(
        "--store",
        action="append",
        help="path to a runtime store (default: every store this host runs)",
    )
    args = parser.parse_args()

    stores = [Path(path) for path in args.store] if args.store else discover_runtime_stores()
    if not stores:
        print(
            "No MiniMax Code runtime store found — MiniMax Code never ran on this host.",
            flush=True,
        )
        return

    missing = [store for store in stores if not store.is_file()]
    if missing:
        for store in missing:
            print(f"No runtime store at {store}.", flush=True)
        return

    init_db()
    with Session(engine) as session:
        for store in stores:
            links = collect_links(store)
            if not links:
                print(f"No background-task rows in {store}.", flush=True)
                continue
            plan = plan_background_attribution(session, links)
            print(
                f"{plan.total:,} background-task row(s) in {store}: "
                f"{len(plan.linked):,} to update, {plan.already_linked:,} already filed, "
                f"{len(plan.missing):,} not collected.",
                flush=True,
            )
            for sample in plan.samples:
                print(f"  {sample}", flush=True)
            if not args.apply:
                continue
            result = apply_background_attribution(session, plan)
            print(
                f"\nDone — updated {result.linked:,} row(s), "
                f"{result.already_linked:,} already filed, {result.missing:,} not collected.",
                flush=True,
            )
        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply to write.", flush=True)


if __name__ == "__main__":
    main()
