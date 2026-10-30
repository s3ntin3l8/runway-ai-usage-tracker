"""Parse ChatGPT/Codex event_msg JSONL logs into UsageEventPush records.

Codex session files use a custom JSONL format:
- type: "turn_context" carries the model (sticky) and reasoning effort
  (non-sticky — applies only to the current turn) for subsequent messages
- type: "event_msg" with payload.type: "token_count" carries per-turn token counts
  in payload.info.last_token_usage

The filename stem is used as the session_id because Codex doesn't embed a session
identifier in individual messages.
"""

import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow importing from app/ when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.source_apps import CODEX  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from scripts.sidecar_pkg import session_names  # noqa: E402


def _normalize_chatgpt_model(model: str) -> str:
    """Normalize a raw Codex model slug to a Runway model_id.

    Preserves the slug verbatim (lowercased/trimmed) so codenamed variants
    (e.g. "gpt-5.6-sol" vs "gpt-5.6-terra") and codex variants (e.g.
    "gpt-5-codex" vs "gpt-5.1-codex-max") stay distinct rather than
    collapsing to a shared bucket. provider_pricing is keyed on the same
    full slug (see app/services/pricing_seed.py); unseeded slugs fall back
    through cost_calculator's family-trim chain rather than losing identity
    here.

    Examples:
        "gpt-5-codex"      → "gpt-5-codex"
        "gpt-5.3-codex"    → "gpt-5.3-codex"
        "gpt-5.6-sol"      → "gpt-5.6-sol"
        "gpt-5.5"          → "gpt-5.5"
        "gpt-5.4-mini"     → "gpt-5.4-mini"
        "gpt-4o"           → "gpt-4o"
        ""                 → "unknown"
    """
    m = (model or "").lower().strip()
    if not m:
        return "unknown"
    return m


def parse_chatgpt_events(
    jsonl_paths: list[Path],
    account_id: str,
    since: datetime,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from Codex JSONL session files.

    Each file is treated as one session. The model is tracked via turn_context
    records and applied to subsequent token_count events. Only token_count events
    with non-None info (i.e. with actual usage data) are emitted.

    Filters to events with ts > since. Deduplicates by (file_stem, line_number)
    because Codex doesn't emit a stable per-message ID.
    """
    events: list[UsageEventPush] = []
    seen: set[str] = set()

    for fp in jsonl_paths:
        try:
            session_id = fp.stem
            current_model = "unknown"
            # Reasoning effort ("low"/"medium"/"high"/"xhigh"/"max"/"ultra"),
            # tracked per turn_context like the model — but NOT sticky: unlike
            # model, a turn_context that omits effort means "no effort set for
            # this turn" (seen for plain gpt-5.4 turns in the same session
            # files as effort-bearing ones), not "reuse the previous turn's
            # effort". Carrying it forward would silently mislabel those turns.
            current_effort: str | None = None
            # cwd + git branch live in the first `session_meta` record and apply
            # to every message in the file.
            current_cwd: str | None = None
            current_branch: str | None = None
            line_number = 0
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    line_number += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue

                    rec_type = record.get("type")

                    # Working dir + git branch come from the session_meta header.
                    if rec_type == "session_meta":
                        payload = record.get("payload", {})
                        current_cwd = payload.get("cwd") or current_cwd
                        git = payload.get("git") or {}
                        if isinstance(git, dict) and git.get("branch"):
                            current_branch = git.get("branch")
                        continue

                    # Track current model from turn_context records. Model is
                    # sticky (a turn_context that omits it inherits the last
                    # one); effort is assigned unconditionally per turn_context
                    # — see the current_effort comment above for why.
                    if rec_type == "turn_context":
                        payload = record.get("payload", {})
                        m = payload.get("model") or payload.get("modelId")
                        if m:
                            current_model = m
                        current_effort = payload.get("effort") or None
                        continue

                    # Only process token_count event_msg records
                    if rec_type != "event_msg":
                        continue
                    payload = record.get("payload", {})
                    if payload.get("type") != "token_count":
                        continue

                    info = payload.get("info")
                    if not info:
                        # token_count with null info — rate-limit-only record, skip
                        continue

                    # Parse timestamp
                    ts_raw = record.get("timestamp") or record.get("ts")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts <= since:
                        continue

                    # Use last_token_usage for per-turn deltas (not cumulative totals).
                    # OpenAI Responses API: input_tokens is inclusive of both
                    # cached_input_tokens and cache_write_input_tokens — subtract both
                    # for fresh-only input, matching Anthropic's column semantics.
                    # (codex-rs/codex-api/src/sse/responses.rs's
                    # parses_cache_write_token_usage test confirms this inclusive
                    # accounting upstream.)
                    last_usage = info.get("last_token_usage") or {}
                    raw_input = int(last_usage.get("input_tokens", 0))
                    tokens_cache_read = int(last_usage.get("cached_input_tokens", 0))
                    tokens_cache_write = int(last_usage.get("cache_write_input_tokens", 0))
                    tokens_input = max(0, raw_input - tokens_cache_read - tokens_cache_write)
                    tokens_output = int(last_usage.get("output_tokens", 0))
                    tokens_reasoning = int(last_usage.get("reasoning_output_tokens", 0))

                    # Synthetic event_id: file stem + line number (stable across replays
                    # as long as the file is not rewritten; Codex appends only)
                    event_id = f"{session_id}|line_{line_number}"
                    if event_id in seen:
                        continue
                    seen.add(event_id)

                    events.append(
                        UsageEventPush(
                            provider_id="chatgpt",
                            source_app=CODEX,
                            account_id=account_id,
                            event_id=event_id,
                            ts=ts.isoformat(),
                            model_id=_normalize_chatgpt_model(current_model),
                            session_id=session_id,
                            cwd=current_cwd,
                            git_branch=current_branch,
                            tokens_input=tokens_input,
                            tokens_output=tokens_output,
                            tokens_cache_read=tokens_cache_read,
                            # input_tokens includes cache_write_input_tokens (same
                            # inclusive accounting as cached_input_tokens, subtracted
                            # above) — gpt-5.6/gpt-6 now have a published cache-write
                            # rate in pricing_seed.py, so forward it instead of zeroing.
                            tokens_cache_create=tokens_cache_write,
                            tokens_reasoning=tokens_reasoning,
                            stop_reason=None,  # not surfaced in token_count events
                            tool_calls=0,
                            effort=current_effort,
                        )
                    )
        except Exception:
            continue

    return events


# ── Session names ─────────────────────────────────────────────────────────────


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def _state_db_order(path: Path) -> tuple[int, str]:
    """Oldest schema version first, so the newest DB's rows win (state_10 after state_9)."""
    digits = path.stem.removeprefix("state_")
    return (int(digits) if digits.isdigit() else -1, path.name)


def _codex_threads(db_path: Path) -> list[tuple[str, str, str | None]]:
    """``(thread id, name, rollout_path)`` of every named thread in a state DB.

    Only the ``name`` column is read for text: ``title``, ``preview`` and
    ``first_user_message`` hold the conversation's first prompt, never a name.
    """
    conn = _open_readonly(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        if not {"id", "name"} <= columns:
            return []
        rollout = "rollout_path" if "rollout_path" in columns else "NULL"
        rows = conn.execute(
            f"SELECT id, name, {rollout} FROM threads WHERE name IS NOT NULL AND name != ''"
        ).fetchall()
    finally:
        conn.close()
    return [
        (str(r[0]), r[1], r[2] if isinstance(r[2], str) else None)
        for r in rows
        if isinstance(r[1], str)
    ]


def _index_thread_names(index_path: Path) -> dict[str, str]:
    """``{thread id: thread_name}`` from ``session_index.jsonl`` (the last row wins)."""
    names: dict[str, str] = {}
    with open(index_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            thread_id, name = row.get("id"), row.get("thread_name")
            if isinstance(thread_id, str) and isinstance(name, str) and name.strip():
                names[thread_id] = name
    return names


def parse_chatgpt_session_names(
    codex_homes: list[Path], rollout_paths: list[Path]
) -> list[dict[str, Any]]:
    """Names of Codex threads, keyed by the rollout file stem the events use.

    ``threads.name`` (kind ``agent``) wins over ``session_index.jsonl``'s
    ``thread_name``. The session id is the stem of the thread's ``rollout_path``;
    without one it is the stem of the discovered rollout file ending in the
    thread id, and a thread with neither is skipped (no guessing).
    """
    stems = {p.stem[-36:]: p.stem for p in rollout_paths if len(p.stem) > 36}
    by_session: dict[str, str] = {}
    for home in codex_homes:
        index_names: dict[str, str] = {}
        index_path = home / "session_index.jsonl"
        if index_path.is_file():
            try:
                index_names = _index_thread_names(index_path)
            except OSError as exc:
                logging.debug("codex session index unreadable (%s)", type(exc).__name__)
        db_names: dict[str, tuple[str, str | None]] = {}
        for db_path in sorted(home.glob("state_*.sqlite"), key=_state_db_order):
            try:
                threads = _codex_threads(db_path)
            except sqlite3.Error as exc:
                logging.debug("codex state db unreadable (%s)", type(exc).__name__)
                continue
            for thread_id, name, rollout_path in threads:
                db_names[thread_id] = (name, rollout_path)
        rollout_by_id = {t: r for t, (_n, r) in db_names.items() if r}
        for thread_id in db_names.keys() | index_names.keys():
            named = db_names.get(thread_id)
            name = named[0] if named and named[0].strip() else index_names.get(thread_id, "")
            if not name.strip():
                continue
            rollout = rollout_by_id.get(thread_id)
            stem = Path(rollout).stem if rollout else stems.get(thread_id[-36:])
            if stem:
                by_session[stem] = name.strip()
    return [
        session_names.make_record("chatgpt", stem, name, "agent", CODEX)
        for stem, name in by_session.items()
    ]
