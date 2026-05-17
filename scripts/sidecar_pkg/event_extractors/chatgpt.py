"""Parse ChatGPT/Codex event_msg JSONL logs into UsageEventPush records.

Codex session files use a custom JSONL format:
- type: "turn_context" carries the model for subsequent messages
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
    """Normalize raw model strings to canonical pricing-table ids.

    Examples:
        "gpt-5-codex"   → "codex"
        "gpt-5.3-codex" → "codex"
        "gpt-5.5"       → "gpt-5.5"
        "gpt-5.4-mini"  → "gpt-5.4-mini"
        "gpt-5.4-nano"  → "gpt-5.4-nano"
        "gpt-5.4-pro"   → "gpt-5.4-pro"
        "gpt-4o"        → "gpt-4o"
        "gpt-4"         → "gpt-4"
        ""              → "unknown"
    """
    m = (model or "").lower().strip()
    if not m:
        return "unknown"
    # Codex-specific model class
    if "codex" in m:
        return "codex"
    # GPT family
    if m.startswith("gpt-"):
        parts = m.split("-")
        if len(parts) >= 2:
            version = parts[1]
            # Append "o" suffix if present (e.g. "gpt-4o")
            if len(parts) >= 3 and parts[2] == "o":
                return f"gpt-{version}o"
            # Preserve size/tier variants (mini/nano/pro)
            if len(parts) >= 3 and parts[2] in ("mini", "nano", "pro"):
                return f"gpt-{version}-{parts[2]}"
            return f"gpt-{version}"
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

                    # Track current model from turn_context records
                    if rec_type == "turn_context":
                        payload = record.get("payload", {})
                        m = payload.get("model") or payload.get("modelId")
                        if m:
                            current_model = m
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
                    # OpenAI Responses API: input_tokens is inclusive of cached_input_tokens
                    # — subtract for fresh-only input to match Anthropic's column semantics.
                    last_usage = info.get("last_token_usage") or {}
                    raw_input = int(last_usage.get("input_tokens", 0))
                    tokens_cache_read = int(last_usage.get("cached_input_tokens", 0))
                    tokens_input = max(0, raw_input - tokens_cache_read)
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
                            tokens_input=tokens_input,
                            tokens_output=tokens_output,
                            tokens_cache_read=tokens_cache_read,
                            tokens_cache_create=0,  # OpenAI doesn't bill cache creation
                            tokens_reasoning=tokens_reasoning,
                            stop_reason=None,  # not surfaced in token_count events
                            tool_calls=0,
                        )
                    )
        except Exception:
            continue

    return events
