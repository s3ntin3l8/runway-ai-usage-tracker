"""Parse Claude assistant-message JSONL logs into UsageEventPush records."""

import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow importing from app/ when running from the repo root.
# sidecar.py does the same via direct execution; here we guard against running
# from a different cwd by inserting the repo root explicitly.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.source_apps import CLAUDE_CODE  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from app.services.model_normalization import normalize_anthropic_model  # noqa: E402
from scripts.sidecar_pkg.session_names import make_record  # noqa: E402

# Kept under its old name for importers inside the sidecar package.
_normalize_anthropic_model = normalize_anthropic_model


def parse_anthropic_events(
    jsonl_paths: list[Path],
    account_id: str,
    since: datetime,
    *,
    ranges: dict[Path, tuple[int, int]] | None = None,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from Claude JSONL log files.

    Filters to assistant messages with ts > since. Deduplicates by (msg_id, req_id).
    """
    seen: set[tuple[str, str]] = set()
    events: list[UsageEventPush] = []
    for fp in jsonl_paths:
        try:
            with open(fp, "rb") as f:
                start, end = ranges[fp] if ranges is not None else (0, None)
                f.seek(start)
                for line in f:
                    if end is not None and f.tell() > end:
                        break
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    try:
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

                        usage = msg.get("usage") or {}
                        content = msg.get("content") or []
                        tool_names = [
                            c["name"]
                            for c in content
                            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name")
                        ]
                        tool_calls = len(tool_names)

                        subagent_type = e.get("attributionAgent") if e.get("isSidechain") else None

                        # Cache writes split by TTL — `cache_creation_input_tokens` is
                        # the total; `cache_creation.{ephemeral_1h,ephemeral_5m}_input_tokens`
                        # is the breakdown cost_calculator needs to bill the 1h portion
                        # at its own (2x input) rate instead of the 5m (1.25x) rate.
                        cache_creation = usage.get("cache_creation") or {}
                        server_tool_use = usage.get("server_tool_use") or {}

                        events.append(
                            UsageEventPush(
                                provider_id="anthropic",
                                source_app=CLAUDE_CODE,
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
                                tokens_cache_create=int(
                                    usage.get("cache_creation_input_tokens", 0)
                                ),
                                tokens_cache_create_1h=int(
                                    cache_creation.get("ephemeral_1h_input_tokens", 0)
                                ),
                                tokens_cache_create_5m=int(
                                    cache_creation.get("ephemeral_5m_input_tokens", 0)
                                ),
                                tokens_reasoning=0,
                                stop_reason=msg.get("stop_reason"),
                                tool_calls=tool_calls,
                                effort=e.get("effort"),
                                speed=usage.get("speed"),
                                service_tier=usage.get("service_tier"),
                                entrypoint=e.get("entrypoint"),
                                app_version=e.get("version"),
                                web_search_requests=int(
                                    server_tool_use.get("web_search_requests", 0)
                                ),
                                web_fetch_requests=int(
                                    server_tool_use.get("web_fetch_requests", 0)
                                ),
                            )
                        )
                        seen.add(dedup_key)
                    except (AttributeError, TypeError, ValueError):
                        continue

        except Exception:
            continue
    return events


_NAME_MARKERS = (b'"agent-name"', b'"ai-title"')

#: Transcript record type -> (record field, session name kind).
_NAME_RECORDS = {"agent-name": ("agentName", "agent"), "ai-title": ("aiTitle", "generated")}


def parse_anthropic_session_names(
    jsonl_paths: list[Path],
    *,
    skip: Callable[[Path], bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[Path, str]]:
    """The last `agent-name` and last `ai-title` of every Claude Code transcript.

    A separate, cheap pass from the event parse: lines are only JSON-decoded
    when they contain a name marker, so the cost is one byte search per line.
    The whole file is read, so a name written after the last event is found.
    Both records carry the `sessionId` the events use (a record without one is skipped);
    `agent-name` is kind `agent` (set by a launcher or `/rename`), `ai-title`
    is kind `generated`.

    `skip(path)` lets the caller leave out a transcript it has already
    covered. Returns `(records, scanned)`, where `scanned` maps each file that
    was read to its `<mtime_ns>:<size>` signature taken *before* the read, so
    a write during the scan is picked up next cycle. Names are never logged.
    """
    records: list[dict[str, Any]] = []
    scanned: dict[Path, str] = {}
    for fp in jsonl_paths:
        try:
            if skip is not None and skip(fp):
                continue
            info = fp.stat()
            signature = f"{info.st_mtime_ns}:{info.st_size}"
            last: dict[str, tuple[str, str]] = {}  # kind -> (session_id, name)
            with open(fp, "rb") as f:
                for line in f:
                    if not any(marker in line for marker in _NAME_MARKERS):
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    spec = _NAME_RECORDS.get(e.get("type")) if isinstance(e, dict) else None
                    if spec is None:
                        continue
                    field, kind = spec
                    name = e.get(field)
                    if not isinstance(name, str) or not name.strip():
                        continue
                    session_id = e.get("sessionId")
                    if not isinstance(session_id, str) or not session_id:
                        # The events carry no stem fallback either, so a name
                        # for it could never be joined to a session.
                        continue
                    last[kind] = (session_id, name.strip())
            for kind, (session_id, name) in last.items():
                records.append(make_record("anthropic", session_id, name, kind, CLAUDE_CODE))
            scanned[fp] = signature
        except OSError:
            continue
    return records, scanned
