"""Parse Gemini CLI session JSONL files into UsageEventPush records.

Gemini session files use a custom JSONL format where each line is a JSON object.
Records with type: "gemini" and a non-empty "tokens" dict are assistant messages.
The file may also contain metadata lines (first line often has sessionId/startTime),
user messages (type: "user"), and $set lines for lastUpdated.

Token fields: tokens.{input, output, cached, thoughts, tool, total}
- input is INCLUSIVE of cached — we subtract to get fresh-only input
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
from app.services.model_normalization import (
    normalize_gemini_model as _normalize_gemini_model,  # noqa: E402,E501
)


def _read_project_root(fp: Path) -> str | None:
    """Recover the project's working directory for a Gemini session file.

    Layout: ``~/.gemini/tmp/<project>/chats/session-*.jsonl`` — the sibling
    ``<project>/.project_root`` file holds the full path. Falls back to the
    ``<project>`` directory name (already the friendly basename), then None.
    """
    try:
        project_dir = fp.parent.parent
        root_file = project_dir / ".project_root"
        if root_file.is_file():
            text = root_file.read_text(encoding="utf-8").strip()
            if text:
                return text
        name = project_dir.name
        return name or None
    except Exception:
        return None


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
            # gemini-cli stores the project root path in a sibling `.project_root`
            # file two levels up (…/<project>/chats/session-*.jsonl). Fall back to
            # the project-dir name, then None.
            session_cwd = _read_project_root(fp)
            # The session "kind" (main/explore/plan) lives on the metadata line;
            # treat non-main as a subagent type.
            session_kind: str | None = None
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

                    # The metadata line (no "type") carries the session kind.
                    if record.get("kind"):
                        session_kind = record.get("kind")

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

                    raw_input = int(raw_tokens.get("input", 0))
                    tokens_cache_read = int(raw_tokens.get("cached", 0))
                    # Gemini's `input` is inclusive of `cached` — subtract for fresh-only.
                    tokens_input = max(0, raw_input - tokens_cache_read)
                    tokens_output = int(raw_tokens.get("output", 0))
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
                            cwd=session_cwd,
                            subagent_type=(
                                session_kind if session_kind and session_kind != "main" else None
                            ),
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
