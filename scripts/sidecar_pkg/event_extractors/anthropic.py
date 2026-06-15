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
    """Map a raw Claude model id to a ``family-version`` slug for grouping.

    The version is preserved so the dashboard can split Opus 4.8 / 4.7 / 4.6.
    The date snapshot token (YYYYMMDD) is dropped so all snapshots of one
    version aggregate. Position-independent, so it handles both the new
    ordering ("claude-opus-4-5-20250929") and the older one
    ("claude-3-5-sonnet-20241022", "claude-3-opus-20240229").

    Examples:
        "claude-opus-4-5-20250929"   -> "opus-4.5"
        "claude-opus-4-7"            -> "opus-4.7"
        "claude-3-5-sonnet-20241022" -> "sonnet-3.5"
        "claude-3-opus-20240229"     -> "opus-3"
        "claude-opus-4-20250514"     -> "opus-4"
        "claude-fable-5"             -> "fable-5"
        "opus"                       -> "opus"
        ""                           -> "unknown"

    Cost lookup falls back to the bare family in
    ``app/services/cost_calculator.py``, so a version with no dedicated
    pricing row still bills at the family rate.
    """
    m = model.lower()
    if "opus" in m:
        family = "opus"
    elif "sonnet" in m:
        family = "sonnet"
    elif "haiku" in m:
        family = "haiku"
    elif "design" in m or "omelette" in m:
        family = "design"
    else:
        # Unknown family (e.g. "claude-fable-5"): take the first segment as the
        # family and fall through to the version logic so the version is kept
        # ("fable-5"), the same way the hard-coded families above are handled.
        base = m.replace("claude-", "")
        if not base:
            return "unknown"
        family = base.split("-")[0]

    # Collect numeric version tokens, dropping the 8-digit date snapshot.
    version_parts = [t for t in m.split("-") if t.isdigit() and len(t) != 8]
    if not version_parts:
        return family
    return f"{family}-{'.'.join(version_parts)}"


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
                    tool_names = [
                        c.get("name")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name")
                    ]
                    tool_calls = len(tool_names)

                    subagent_type = e.get("attributionAgent") if e.get("isSidechain") else None

                    events.append(
                        UsageEventPush(
                            provider_id="anthropic",
                            account_id=account_id,
                            event_id=event_id,
                            ts=ts.isoformat(),
                            model_id=_normalize_anthropic_model(msg.get("model", "")),
                            session_id=e.get("sessionId"),
                            cwd=e.get("cwd"),
                            git_branch=e.get("gitBranch"),
                            tool_names=tool_names or None,
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
