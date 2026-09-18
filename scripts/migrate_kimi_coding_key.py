#!/usr/bin/env python3
"""One-shot migration: kimi_coding API key stored in the session_cookie slot.

Background
----------
Before the Kimi Coding collector gained API-key support, the Settings UI only
offered an "Auth Token (web)" field, so users pasted their Kimi For Coding API
key (from kimi.com/code/console) into the session_cookie column. The key works
against the Code API (GET api.kimi.com/coding/v1/usages) but is sent to the
wrong endpoint from that slot; the web gateway only accepts kimi-auth JWTs
("token contains an invalid number of segments" otherwise).

This script moves such values to the api_key column so the new api strategy
picks them up:
  - session_cookie is set, api_key is empty, AND
  - the value is NOT a JWT (a real kimi-auth cookie has three dot-separated
    segments and stays in the session_cookie slot for the web strategy).

Values that look like JWTs are left untouched — they are legitimate web
cookies for the web strategy.

Run with the server STOPPED (SQLite is single-writer) and APP_HOST=127.0.0.1:

  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/migrate_kimi_coding_key.py --dry-run
  # eyeball the plan, then:
  RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \\
      python scripts/migrate_kimi_coding_key.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.models.db import ProviderConfig  # noqa: E402


def _looks_like_jwt(value: str) -> bool:
    parts = value.strip().split(".")
    return len(parts) == 3 and all(parts)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Report the plan, write nothing.")
    g.add_argument("--apply", action="store_true", help="Move the misfiled key(s).")
    args = p.parse_args()
    dry_run = args.dry_run
    prefix = "[DRY-RUN] " if dry_run else ""

    with Session(engine) as session:
        rows = session.exec(
            select(ProviderConfig).where(ProviderConfig.provider_id == "kimi_coding")
        ).all()

        moved = 0
        for row in rows:
            cookie = row.session_cookie
            if not cookie or row.api_key:
                continue
            if _looks_like_jwt(cookie):
                print(
                    f"{prefix}skip {row.provider_id}/{row.account_id}: session_cookie "
                    "looks like a kimi-auth JWT (web strategy) — left in place.",
                    flush=True,
                )
                continue
            print(
                f"{prefix}{row.provider_id}/{row.account_id}: session_cookie -> api_key "
                f"({len(cookie)} chars, non-JWT)",
                flush=True,
            )
            if not dry_run:
                row.api_key = cookie
                row.session_cookie = None
                session.add(row)
            moved += 1

        if not dry_run and moved:
            session.commit()

        verb = "would be moved" if dry_run else "moved"
        print(f"\n{prefix}{moved} credential(s) {verb}.", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
