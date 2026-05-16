"""Backfill usage_events.subagent_type for historical Anthropic events.

Scans every ~/.claude/projects/**/*.jsonl, finds assistant lines with
isSidechain=true + attributionAgent, and UPDATEs the corresponding rows
in usage_events. Idempotent: only touches rows where subagent_type IS NULL.

Run once after deploying the schema change. Forward-going ingestion is
already correct via the updated anthropic extractor.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Allow running from repo root: `python scripts/backfill_subagent_type.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.db import engine, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_subagent_type")


def _candidate_dirs() -> list[Path]:
    dirs: list[str] = []
    config_env = os.getenv("CLAUDE_CONFIG_DIR", "")
    if config_env:
        for p in config_env.split(","):
            p = p.strip()
            if not p:
                continue
            proj = os.path.join(p, "projects") if not p.endswith("/projects") else p
            dirs.append(proj)
    dirs.extend(
        [
            os.path.expanduser("~/.claude/projects"),
            os.path.expanduser("~/.config/claude/projects"),
        ]
    )
    return [Path(d) for d in dirs if os.path.isdir(d)]


def _collect_pairs(jsonl: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    try:
        with open(jsonl, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") != "assistant":
                    continue
                if not e.get("isSidechain"):
                    continue
                atype = e.get("attributionAgent")
                if not atype:
                    continue
                mid = (e.get("message") or {}).get("id")
                if not mid:
                    continue
                pairs.append((mid, atype))
    except Exception as exc:
        logger.warning("skip %s: %s", jsonl, exc)
    return pairs


def main() -> int:
    init_db()  # ensure column exists

    dirs = _candidate_dirs()
    if not dirs:
        logger.error("No Claude projects directory found")
        return 1

    all_pairs: dict[str, str] = {}
    for d in dirs:
        for jsonl in d.glob("**/*.jsonl"):
            for mid, atype in _collect_pairs(jsonl):
                all_pairs.setdefault(mid, atype)

    logger.info("Found %d distinct subagent msg_ids across %d dir(s)", len(all_pairs), len(dirs))

    if not all_pairs:
        return 0

    updated = 0
    with engine.connect() as conn:
        for mid, atype in all_pairs.items():
            result = conn.execute(
                text(
                    "UPDATE usage_events SET subagent_type = :t "
                    "WHERE provider_id = 'anthropic' "
                    "AND (event_id = :mid OR event_id LIKE :mid_pipe) "
                    "AND subagent_type IS NULL"
                ),
                {"t": atype, "mid": mid, "mid_pipe": f"{mid}|%"},
            )
            updated += result.rowcount or 0
        conn.commit()

    logger.info("Updated %d rows", updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
