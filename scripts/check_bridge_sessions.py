#!/usr/bin/env python3
"""Compare bridge-session cloud usage blocks with Runway's local events (offline).

``--rows rows.json`` is a JSON list of ``{session_id, input, cache_read, cache_write}``
(any value may be null). The script reads the configured database read-only, makes no
provider call, and prints a counts-only JSON report (no ids). See docs/collection_logic.md.

  python scripts/check_bridge_sessions.py --rows rows.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.services.claude_cloud_bridge_check import (  # noqa: E402
    compare_bridge_blocks,
    load_local_totals,
)
from app.services.claude_cloud_probe import session_key  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", required=True, type=Path, help="JSON file of list rows")
    args = parser.parse_args(argv)

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
