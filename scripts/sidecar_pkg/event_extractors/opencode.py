"""Parse OpenCode SQLite database into UsageEventPush records.

OpenCode stores messages in a SQLite database (~/.local/share/opencode/opencode.db).
Each assistant message row in the `message` table has:
  - id: TEXT primary key (e.g. "msg_<hash>")
  - session_id: TEXT (direct column, not in JSON)
  - time_created: INTEGER (Unix milliseconds)
  - data: TEXT (JSON blob)

The data JSON contains:
  - role: "assistant" | "user"
  - modelID: model name string
  - providerID: "opencode-go" | "opencode"
  - cost: float (USD — authoritative; skip pricing table lookup)
  - tokens: {input, output, reasoning, cache: {read, write}, total}
  - finish: stop reason

Because OpenCode logs the cost directly per message, these events carry
cost_usd and the server's EventIngestor should use that value rather than
computing from the pricing table.
"""

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# Allow importing from app/ when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.schemas import UsageEventPush  # noqa: E402


def parse_opencode_events(
    db_path: Path,
    account_id: str,
    since: datetime,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from an OpenCode SQLite database.

    Reads all assistant messages with time_created > since from the message table.
    Deduplicates by message id (the primary key is stable).
    Sets cost_usd from the logged cost field (skip pricing table lookup).

    Args:
        db_path: Path to the opencode.db SQLite file.
        account_id: Canonical account email or "default".
        since: Only return events strictly after this timestamp.
    """
    if not db_path.exists():
        return []

    # Convert since to millisecond epoch for SQLite comparison
    since_ms = int(since.timestamp() * 1000)

    events: list[UsageEventPush] = []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, session_id, time_created, data
                FROM message
                WHERE time_created > ?
                  AND json_extract(data, '$.role') = 'assistant'
                ORDER BY time_created ASC
                """,
                (since_ms,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    for row in rows:
        msg_id, session_id, time_created_ms, data_json = row
        try:
            data = json.loads(data_json) if data_json else {}
        except Exception:
            continue

        # Convert ms epoch to datetime
        try:
            ts = datetime.fromtimestamp(int(time_created_ms) / 1000.0, tz=UTC)
        except Exception:
            continue

        # Extract token fields from the nested tokens dict
        raw_tokens = data.get("tokens") or {}
        tokens_input = int(raw_tokens.get("input", 0))
        tokens_output = int(raw_tokens.get("output", 0))
        tokens_reasoning = int(raw_tokens.get("reasoning", 0))
        cache = raw_tokens.get("cache") or {}
        tokens_cache_read = int(cache.get("read", 0))
        tokens_cache_create = int(cache.get("write", 0))

        # Cost is authoritative — skip pricing table
        cost_usd = float(data.get("cost") or 0.0)

        model_id = data.get("modelID") or "unknown"
        stop_reason = data.get("finish") or None

        # Use the row's id (stable primary key) as event_id
        event_id = msg_id or f"opencode|{session_id or 'unknown'}|ts_{time_created_ms}"

        events.append(
            UsageEventPush(
                provider_id="opencode",
                account_id=account_id,
                event_id=event_id,
                ts=ts.isoformat(),
                model_id=model_id,
                session_id=session_id or None,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                tokens_cache_read=tokens_cache_read,
                tokens_cache_create=tokens_cache_create,
                tokens_reasoning=tokens_reasoning,
                stop_reason=stop_reason,
                tool_calls=0,
                cost_usd=cost_usd,
            )
        )

    return events
