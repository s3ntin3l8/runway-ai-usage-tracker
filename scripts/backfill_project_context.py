#!/usr/bin/env python3
"""Backfill working-directory / project context onto existing usage_events.

Phase 2 added cwd / project / git_branch / tools_json (and started populating
latency_ms) on usage_events, sourced from the provider logs. Rows ingested
before that land with these columns NULL. This script re-reads the on-disk
logs, re-runs the (updated) extractors, and patches the missing context onto
matching rows so history is populated without waiting for re-ingest.

Matching is by (provider_id, event_id) — event_ids are already provider-unique.
The UPDATE is guarded by `cwd IS NULL`, so the script is idempotent and never
clobbers a row that already has context (or one corrected by hand).

Run against the dev DB or a snapshot — never the live prod DB while the server
is writing (SQLite is single-writer):

  APP_HOST=127.0.0.1 python scripts/backfill_project_context.py            # all providers
  APP_HOST=127.0.0.1 python scripts/backfill_project_context.py --dry-run
  APP_HOST=127.0.0.1 python scripts/backfill_project_context.py --provider anthropic
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from app.core.db import engine, init_db  # noqa: E402
from app.models.db import UsageEvent  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from app.services.project_label import derive_project  # noqa: E402
from scripts.sidecar import (  # noqa: E402
    _discover_anthropic_log_paths,
    _discover_codex_log_paths,
    _discover_gemini_log_paths,
    _discover_opencode_db_path,
)
from scripts.sidecar_pkg.event_extractors.anthropic import parse_anthropic_events  # noqa: E402
from scripts.sidecar_pkg.event_extractors.chatgpt import parse_chatgpt_events  # noqa: E402
from scripts.sidecar_pkg.event_extractors.gemini import parse_gemini_events  # noqa: E402
from scripts.sidecar_pkg.event_extractors.opencode import parse_opencode_events  # noqa: E402

# Reach back far enough to cover all retained history.
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def _collect_pushes(provider: str) -> dict[str, UsageEventPush]:
    """Re-parse a provider's on-disk logs → {event_id: push}."""
    if provider == "anthropic":
        pushes = parse_anthropic_events(_discover_anthropic_log_paths(), "backfill", _EPOCH)
    elif provider == "chatgpt":
        pushes = parse_chatgpt_events(_discover_codex_log_paths(), "backfill", _EPOCH)
    elif provider == "gemini":
        pushes = parse_gemini_events(_discover_gemini_log_paths(), "backfill", _EPOCH)
    elif provider == "opencode":
        db = _discover_opencode_db_path()
        pushes = parse_opencode_events(db, "backfill", _EPOCH) if db else []
    else:
        return {}
    return {p.event_id: p for p in pushes}


def _has_context(push: UsageEventPush) -> bool:
    """True when the push carries any new context worth writing."""
    return push.cwd is not None or push.tool_names is not None or push.latency_ms is not None


def _apply(ev: UsageEvent, push: UsageEventPush) -> None:
    """Apply context from `push` to a NULL-cwd event (mirrors EventIngestor)."""
    ev.cwd = push.cwd
    ev.project = derive_project(push.cwd)
    ev.git_branch = push.git_branch
    ev.tools_json = json.dumps(push.tool_names) if push.tool_names else None
    if push.latency_ms is not None:
        ev.latency_ms = push.latency_ms


# OpenCode splits into several runway providers at ingest (Go, free,
# bring-your-own-key, OpenRouter, Ollama Cloud, ...) — see
# map_opencode_provider_id in scripts/sidecar_pkg/event_extractors/opencode.py.
# All of them map back to the same "opencode" log source (the sqlite DB), so
# use the id prefix rather than an exact-id set to also cover new siblings.
_OPENCODE_PROVIDER_PREFIX = "opencode"
_PROVIDERS = [
    "anthropic",
    "chatgpt",
    "gemini",
    "opencode",
    "opencode-free",
    "opencode-byok",
    "opencode-openrouter",
    "opencode-ollama",
]


def backfill(session: Session, providers: list[str], dry_run: bool) -> int:
    total_changed = 0
    # Cache parsed logs per source so opencode/opencode-free share one parse.
    push_cache: dict[str, dict[str, UsageEventPush]] = {}

    for provider in providers:
        source = "opencode" if provider.startswith(_OPENCODE_PROVIDER_PREFIX) else provider
        pushes = push_cache.setdefault(source, _collect_pushes(source))
        if not pushes:
            print(f"{provider}: no log events found, skipping", flush=True)
            continue

        events = session.exec(
            select(UsageEvent).where(
                UsageEvent.provider_id == provider,
                UsageEvent.cwd.is_(None),  # type: ignore[attr-defined]
            )
        ).all()
        changed = 0
        for ev in events:
            push = pushes.get(ev.event_id)
            if not push or not _has_context(push):
                continue
            changed += 1
            if not dry_run:
                _apply(ev, push)
                session.add(ev)
        print(
            f"{provider}: {changed:,} of {len(events):,} NULL-cwd event(s) matched"
            + (" (dry-run)" if dry_run else ""),
            flush=True,
        )
        total_changed += changed

    if not dry_run and total_changed:
        session.commit()
    return total_changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", help="limit to one runway provider id")
    parser.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    args = parser.parse_args()

    init_db()
    providers = [args.provider] if args.provider else _PROVIDERS

    with Session(engine) as session:
        changed = backfill(session, providers, args.dry_run)

    verb = "would update" if args.dry_run else "updated"
    print(f"\nDone — {verb} {changed:,} event(s).", flush=True)


if __name__ == "__main__":
    main()
