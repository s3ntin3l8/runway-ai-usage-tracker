"""Parse Claude assistant-message JSONL logs into UsageEventPush records."""

import json
import sys
from datetime import datetime
from pathlib import Path

# Allow importing from app/ when running from the repo root.
# sidecar.py does the same via direct execution; here we guard against running
# from a different cwd by inserting the repo root explicitly.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.schemas import UsageEventPush  # noqa: E402


def _normalize_anthropic_model(model: str) -> str:
    """Map 'claude-sonnet-4-5-20250929' to 'sonnet' for display grouping.

    Keep separate buckets for 'design' if present; full model id stored in raw_json.
    """
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    if "design" in m or "omelette" in m:
        return "design"
    base = m.replace("claude-", "")
    return base.split("-")[0] if base else "unknown"


def parse_anthropic_events(
    jsonl_paths: list[Path],
    account_id: str,
    since: datetime,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from Claude JSONL log files.

    Filters to assistant messages with ts > since. Deduplicates by (msg_id, req_id).
    """
    seen: set[tuple[str, str]] = set()
    events: list[UsageEventPush] = []
    for fp in jsonl_paths:
        try:
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("type") != "assistant":
                        continue
                    ts_raw = e.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts <= since:
                        continue

                    msg = e.get("message", {})
                    msg_id = msg.get("id") or ""
                    req_id = msg.get("requestId") or ""
                    event_id = f"{msg_id}|{req_id}" if req_id else msg_id
                    if not event_id:
                        continue
                    dedup_key = (msg_id, req_id)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    usage = msg.get("usage") or {}
                    content = msg.get("content") or []
                    tool_calls = sum(
                        1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use"
                    )

                    subagent_type = (
                        e.get("attributionAgent") if e.get("isSidechain") else None
                    )

                    events.append(
                        UsageEventPush(
                            provider_id="anthropic",
                            account_id=account_id,
                            event_id=event_id,
                            ts=ts.isoformat(),
                            model_id=_normalize_anthropic_model(msg.get("model", "")),
                            session_id=e.get("sessionId"),
                            subagent_type=subagent_type,
                            tokens_input=int(usage.get("input_tokens", 0)),
                            tokens_output=int(usage.get("output_tokens", 0)),
                            tokens_cache_read=int(usage.get("cache_read_input_tokens", 0)),
                            tokens_cache_create=int(usage.get("cache_creation_input_tokens", 0)),
                            tokens_reasoning=0,
                            stop_reason=msg.get("stop_reason"),
                            tool_calls=tool_calls,
                        )
                    )
        except Exception:
            continue
    return events
