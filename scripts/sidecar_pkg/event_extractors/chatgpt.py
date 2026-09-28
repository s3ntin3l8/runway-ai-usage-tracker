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
import sys
from datetime import datetime
from pathlib import Path

# Allow importing from app/ when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.schemas import UsageEventPush  # noqa: E402


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
