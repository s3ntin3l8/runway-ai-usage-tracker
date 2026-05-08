"""Parse Gemini CLI session JSONL files into UsageEventPush records.

Gemini session files use a custom JSONL format where each line is a JSON object.
Records with type: "gemini" and a non-empty "tokens" dict are assistant messages.
The file may also contain metadata lines (first line often has sessionId/startTime),
user messages (type: "user"), and $set lines for lastUpdated.

Token fields: tokens.{input, output, cached, thoughts, tool, total}
- cached  → tokens_cache_read
- thoughts → tokens_reasoning

The "id" field on each gemini record is used as event_id when present;
otherwise a synthetic "stem|line_N" is assigned.
The filename stem is used as session_id.
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


def _normalize_gemini_model(model_name: str) -> str:
    """Map raw Gemini model strings to short display buckets.

    Examples:
        "gemini-2.5-flash"      → "flash"
        "gemini-2.5-pro"        → "pro"
        "gemini-2.5-flash-lite" → "flash-lite"
        "gemini-3-flash-preview"→ "flash"
        ""                       → "unknown"
    """
    lower = (model_name or "").lower()
    if not lower:
        return "unknown"
    if "flash-lite" in lower:
        return "flash-lite"
    if "flash" in lower:
        return "flash"
    if "pro" in lower:
        return "pro"
    if "ultra" in lower:
        return "ultra"
    return model_name or "unknown"


def parse_gemini_events(
    jsonl_paths: list[Path],
    account_id: str,
    since: datetime,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from Gemini CLI JSONL session files.

    Filters to gemini-type records with tokens and ts > since.
    Deduplicates by event_id (record's "id" field, or synthetic stem|line_N).
    """
    events: list[UsageEventPush] = []
    seen: set[str] = set()

    for fp in jsonl_paths:
        try:
            session_id = fp.stem
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

                    # Only process gemini assistant records with token data
                    if record.get("type") != "gemini":
                        continue
                    raw_tokens = record.get("tokens")
                    if not raw_tokens:
                        continue

                    # Parse timestamp
                    ts_raw = record.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts <= since:
                        continue

                    # event_id: prefer record's own "id"; fall back to synthetic
                    record_id = record.get("id")
                    event_id = record_id if record_id else f"{session_id}|line_{line_number}"
                    if event_id in seen:
                        continue
                    seen.add(event_id)

                    tokens_input = int(raw_tokens.get("input", 0))
                    tokens_output = int(raw_tokens.get("output", 0))
                    tokens_cache_read = int(raw_tokens.get("cached", 0))
                    tokens_reasoning = int(raw_tokens.get("thoughts", 0))
                    # Gemini doesn't bill cache creation separately
                    tokens_cache_create = 0

                    raw_model = record.get("model") or "unknown"

                    events.append(
                        UsageEventPush(
                            provider_id="gemini",
                            account_id=account_id,
                            event_id=event_id,
                            ts=ts.isoformat(),
                            model_id=_normalize_gemini_model(raw_model),
                            session_id=session_id,
                            tokens_input=tokens_input,
                            tokens_output=tokens_output,
                            tokens_cache_read=tokens_cache_read,
                            tokens_cache_create=tokens_cache_create,
                            tokens_reasoning=tokens_reasoning,
                            stop_reason=None,  # not surfaced in Gemini CLI logs
                            tool_calls=0,
                        )
                    )
        except Exception:
            continue

    return events
