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
  - providerID: "opencode-go" | "opencode" | "open-design-byok" | "openrouter"
    | "ollama-cloud" | ... (OpenCode's own backend/billing-tier tag — NOT the
    upstream model maker)
  - cost: float (USD — authoritative; skip pricing table lookup)
  - tokens: {input, output, reasoning, cache: {read, write}, total}
  - finish: stop reason
  - error: {name, data: {message, statusCode, ...}} — present when the
    request to the upstream backend failed (no tokens/cost were incurred)

Because OpenCode logs the cost directly per message, these events carry
cost_usd and the server's EventIngestor should use that value rather than
computing from the pricing table.

providerID -> runway provider_id mapping (see _OC_PROVIDER_MAP below):
  - "opencode"          -> "opencode-free"      (free-tier models)
  - "opencode-go"       -> "opencode"           (paid Go subscription; carries
                                                   the web-scraper quota gauges)
  - "open-design-byok"  -> "opencode-byok"      (bring-your-own-key)
  - "openrouter"        -> "opencode-openrouter"
  - "ollama-cloud"      -> "opencode-ollama"
  - anything else       -> "opencode-<slug>"    (never silently folds into Go)
  - missing/empty       -> "opencode"           (historical default)

Messages whose `error` field is set are pushed with kind="error" (no tokens/
cost were actually incurred) so they don't inflate usage totals on whichever
card they land on.
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

# OpenCode's own providerID (backend/billing tier) -> runway provider_id.
# Keep this in sync with scripts/reclassify_opencode_providers.py, which
# reapplies the same mapping to already-ingested events.
_OC_PROVIDER_MAP: dict[str, str] = {
    "opencode": "opencode-free",
    "opencode-go": "opencode",
    "open-design-byok": "opencode-byok",
    "openrouter": "opencode-openrouter",
    "ollama-cloud": "opencode-ollama",
}


def map_opencode_provider_id(oc_provider_id: str) -> str:
    """Map an OpenCode `providerID` to a runway `provider_id`.

    Unknown/new backends get their own derived `opencode-<slug>` id rather
    than silently collapsing into the Go tier (the bug in issue #182). A
    missing/empty providerID keeps the historical default of "opencode".
    """
    oc_provider_id = (oc_provider_id or "").strip().lower()
    if not oc_provider_id:
        return "opencode"
    return _OC_PROVIDER_MAP.get(oc_provider_id, f"opencode-{oc_provider_id}")


def _classify_opencode_error(err: dict) -> str:
    """Best-effort short tag for an OpenCode message-level error.

    `err` is the raw `data.error` dict, e.g.
    `{"name": "APIError", "data": {"statusCode": 401, "message": "..."}}`.
    """
    status = None
    err_data = err.get("data")
    if isinstance(err_data, dict):
        status = err_data.get("statusCode")

    if status == 401:
        return "auth_failed"
    if status == 403:
        return "quota_exceeded"
    if status == 429:
        return "rate_limit"
    if status in (408, 504):
        return "timeout"
    if isinstance(status, int):
        return f"http_{status}"

    name = err.get("name")
    return str(name).lower() if name else "unknown_error"


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

        # Working directory (path.cwd, falling back to the repo root) and request
        # latency (completed − created, both ms epoch) — OpenCode is the only
        # provider that logs message timing.
        path = data.get("path") or {}
        cwd = path.get("cwd") or path.get("root")
        time_obj = data.get("time") or {}
        created = time_obj.get("created")
        completed = time_obj.get("completed")
        latency_ms = (
            int(completed - created)
            if isinstance(created, int | float)
            and isinstance(completed, int | float)
            and completed >= created
            else None
        )

        # Use the row's id (stable primary key) as event_id
        event_id = msg_id or f"opencode|{session_id or 'unknown'}|ts_{time_created_ms}"

        # OpenCode tags each message with a providerID identifying which
        # backend/billing tier served it (Go subscription, free models,
        # bring-your-own-key, OpenRouter, Ollama Cloud, ...). Map it to a
        # dedicated runway provider_id so no backend is ever silently folded
        # into the Go tier (issue #182).
        oc_provider_id = data.get("providerID") or ""
        runway_provider_id = map_opencode_provider_id(oc_provider_id)

        # A failed request (bad auth, no subscription, etc.) never actually
        # incurred usage — push it as kind="error" so it doesn't inflate
        # message/token counts on whichever card it lands on.
        err = data.get("error")
        if isinstance(err, dict):
            events.append(
                UsageEventPush(
                    provider_id=runway_provider_id,
                    account_id=account_id,
                    event_id=event_id,
                    ts=ts.isoformat(),
                    model_id=model_id,
                    session_id=session_id or None,
                    cwd=cwd,
                    kind="error",
                    error_reason=_classify_opencode_error(err),
                )
            )
            continue

        events.append(
            UsageEventPush(
                provider_id=runway_provider_id,
                account_id=account_id,
                event_id=event_id,
                ts=ts.isoformat(),
                model_id=model_id,
                session_id=session_id or None,
                cwd=cwd,
                latency_ms=latency_ms,
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
