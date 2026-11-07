#!/usr/bin/env python3
"""Compare bridge-session cloud usage blocks with Runway's local events (offline; --live reads the provider).

``--rows rows.json`` is a JSON list of ``{session_id, input, cache_read, cache_write}``
(any value may be null). The script reads the configured database read-only, makes no
provider call, and prints a counts-only JSON report (no ids). See docs/collection_logic.md.

``--live`` instead pages ``GET /v1/code/sessions`` with the cached Claude Code login (the
collector's bounds, read-only, no refresh). It calls the provider, so it also requires
``--yes-call-provider`` and refuses to run without it. The login lives in the server
process's token cache, so ``--live`` hydrates it from the durable contributions in the database.

  python scripts/check_bridge_sessions.py --rows rows.json
  python scripts/check_bridge_sessions.py --live --yes-call-provider
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.services import source_controls  # noqa: E402
from app.services.claude_cloud_bridge_check import (  # noqa: E402
    compare_bridge_blocks,
    load_local_totals,
)
from app.services.claude_cloud_bridge_rows import BridgeRow, read_all_bridge_rows  # noqa: E402
from app.services.claude_cloud_probe import session_key  # noqa: E402
from app.services.claude_cloud_support import ProbeError  # noqa: E402


async def _live_rows() -> list[BridgeRow]:
    # A fresh process hydrates its login cache only from the durable snapshot, which is read
    # only once source controls are enabled (the server does this in its lifespan).
    source_controls.initialize()
    async with httpx.AsyncClient() as client:
        return await read_all_bridge_rows(client)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rows", type=Path, help="JSON file of list rows")
    source.add_argument("--live", action="store_true", help="read the rows from the live list")
    parser.add_argument(
        "--yes-call-provider",
        action="store_true",
        help="authorize the provider call that --live makes",
    )
    args = parser.parse_args(argv)

    if args.live:
        if not args.yes_call_provider:
            print(
                "--live calls the provider: pass --yes-call-provider to authorize it",
                file=sys.stderr,
            )
            return 2
        try:
            rows: list = asyncio.run(_live_rows())
        except ProbeError as exc:
            # A fixed outcome name only, never a response value.
            print(f"live read failed: {exc}", file=sys.stderr)
            return 3
    else:
        if args.yes_call_provider:
            print("--yes-call-provider only applies to --live", file=sys.stderr)
            return 2
        rows = json.loads(args.rows.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(
        isinstance(r, dict) and isinstance(r.get("session_id"), str) for r in rows
    ):
        print("rows must be a JSON list of objects with a string session_id", file=sys.stderr)
        return 2
    with Session(engine) as session:
        totals = load_local_totals(session, [session_key(r["session_id"]) for r in rows])
    print(json.dumps(compare_bridge_blocks(rows, totals).to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
