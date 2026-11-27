"""Parse Claude assistant-message JSONL logs into UsageEventPush records."""

import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
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
from scripts.sidecar_pkg.event_watermark import EventWatermark, transcript_fingerprint  # noqa: E402
from scripts.sidecar_pkg.session_names import make_record  # noqa: E402
from scripts.sidecar_pkg.transcript_audit import (  # noqa: E402
    AUDIT_BYTES,
    AUDIT_FIELDS,
    audit,
    chunks_of,
    extend_chunks,
)

# Kept under its old name for importers inside the sidecar package.
_normalize_anthropic_model = normalize_anthropic_model


def _build_advisor_events(
    main_event: UsageEventPush,
    iterations: list[Any],
) -> list[UsageEventPush]:
    """Build one UsageEventPush per advisor model from `usage.iterations`."""
    totals_by_model: dict[str, list[int]] = {}
    for entry in iterations:
        if not isinstance(entry, dict) or entry.get("type") != "advisor_message":
            continue
        raw_model = entry.get("model")
        model_id = _normalize_anthropic_model(raw_model if isinstance(raw_model, str) else "")
        cache_creation = entry.get("cache_creation")
        if not isinstance(cache_creation, dict):
            cache_creation = {}
        try:
            counts = (
                int(entry.get("input_tokens") or 0),
                int(entry.get("output_tokens") or 0),
                int(entry.get("cache_read_input_tokens") or 0),
                int(entry.get("cache_creation_input_tokens") or 0),
                int(cache_creation.get("ephemeral_1h_input_tokens") or 0),
                int(cache_creation.get("ephemeral_5m_input_tokens") or 0),
            )
        except (TypeError, ValueError):
            continue
        acc = totals_by_model.get(model_id)
        if acc is None:
            totals_by_model[model_id] = list(counts)
        else:
            for i, val in enumerate(counts):
                acc[i] += val

    if not totals_by_model:
        return []

    multi_model = len(totals_by_model) > 1
    advisor_events: list[UsageEventPush] = []
    for model_id, (
        inp,
        out,
        cache_read,
        cache_create,
        cache_1h,
        cache_5m,
    ) in totals_by_model.items():
        suffix = f"|advisor:{model_id}" if multi_model else "|advisor"
        advisor_events.append(
            UsageEventPush(
                provider_id="anthropic",
                source_app=main_event.source_app,
                account_id=main_event.account_id,
                event_id=f"{main_event.event_id}{suffix}",
                ts=main_event.ts,
                model_id=model_id,
                session_id=main_event.session_id,
                request_id=main_event.request_id,
                turn_id=main_event.event_id,
                cwd=main_event.cwd,
                git_branch=main_event.git_branch,
                tool_names=None,
                tokens_input=inp,
                tokens_output=out,
                streamed_usage=True,
                tokens_cache_read=cache_read,
                tokens_cache_create=cache_create,
                tokens_cache_create_1h=cache_1h,
                tokens_cache_create_5m=cache_5m,
                tokens_reasoning=0,
                subagent_type=main_event.subagent_type,
                entrypoint=main_event.entrypoint,
                app_version=main_event.app_version,
            )
        )
    return advisor_events


def parse_anthropic_events(
    jsonl_paths: list[Path],
    account_id: str,
    since: datetime,
    *,
    ranges: dict[Path, tuple[int, int]] | None = None,
    progress: dict[Path, int] | None = None,
    floors: dict[Path, datetime] | None = None,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from Claude JSONL log files.

    Filters to assistant messages with ts > since. Deduplicates by (msg_id, req_id),
    taking usage from the largest output count (later record on ties), while
    preserving the first record's timestamp and identity.
    """
    events: dict[tuple[str, str], UsageEventPush] = {}
    winning_iterations: dict[tuple[str, str], list[Any]] = {}
    for fp in jsonl_paths:
        try:
            with open(fp, "rb") as f:
                start, end = ranges[fp] if ranges is not None else (0, None)
                f.seek(start)
                for line in f:
                    if end is not None and f.tell() > end:
                        break
                    if progress is not None and line.endswith(b"\n"):
                        # A valid final JSON record can lack a newline. Parse it
                        # now, but replay that line if the file grows.
                        progress[fp] = f.tell()
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
                        if ts <= (floors[fp] if floors is not None else since):
                            continue

                        msg = e.get("message", {})
                        msg_id = msg.get("id") or ""
                        # event_id and dedup_key intentionally read only msg.get("requestId"):
                        # stored rows are keyed by (provider_id, event_id), so adding
                        # top-level e["requestId"] to event_id would make every existing
                        # event look new and double-count history on the next sidecar run.
                        req_id = msg.get("requestId") or ""
                        event_id = f"{msg_id}|{req_id}" if req_id else msg_id
                        if not event_id:
                            continue
                        dedup_key = (msg_id, req_id)
                        raw_request_id = e.get("requestId") or msg.get("requestId")
                        request_id = (
                            raw_request_id
                            if isinstance(raw_request_id, str) and raw_request_id
                            else None
                        )

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

                        event = UsageEventPush(
                            provider_id="anthropic",
                            source_app=CLAUDE_CODE,
                            account_id=account_id,
                            event_id=event_id,
                            ts=ts.isoformat(),
                            model_id=_normalize_anthropic_model(msg.get("model", "")),
                            session_id=e.get("sessionId"),
                            request_id=request_id,
                            cwd=e.get("cwd"),
                            git_branch=e.get("gitBranch"),
                            tool_names=tool_names or None,
                            subagent_type=subagent_type,
                            tokens_input=int(usage.get("input_tokens", 0)),
                            tokens_output=int(usage.get("output_tokens", 0)),
                            streamed_usage=True,
                            tokens_cache_read=int(usage.get("cache_read_input_tokens", 0)),
                            tokens_cache_create=int(usage.get("cache_creation_input_tokens", 0)),
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
                            web_search_requests=int(server_tool_use.get("web_search_requests", 0)),
                            web_fetch_requests=int(server_tool_use.get("web_fetch_requests", 0)),
                        )
                        raw_iterations = usage.get("iterations")
                        iterations = raw_iterations if isinstance(raw_iterations, list) else None
                        first = events.get(dedup_key)
                        if first is None:
                            events[dedup_key] = event
                            if iterations:
                                winning_iterations[dedup_key] = iterations
                        elif event.tokens_output >= first.tokens_output:
                            # Streaming updates replace usage as a unit. Keep the
                            # first timestamp/identity so the event stays on its day.
                            events[dedup_key] = first.model_copy(
                                update={
                                    field: getattr(event, field)
                                    for field in (
                                        "tokens_input",
                                        "tokens_output",
                                        "tokens_cache_read",
                                        "tokens_cache_create",
                                        "tokens_cache_create_1h",
                                        "tokens_cache_create_5m",
                                        "speed",
                                        "service_tier",
                                        "stop_reason",
                                        "tool_names",
                                        "tool_calls",
                                        "web_search_requests",
                                        "web_fetch_requests",
                                    )
                                }
                            )
                            if iterations:
                                winning_iterations[dedup_key] = iterations
                            else:
                                winning_iterations.pop(dedup_key, None)
                    except (AttributeError, TypeError, ValueError):
                        continue

        except Exception:
            if progress is not None:
                # Other transcripts can still be delivered. Keep this one's
                # last accepted position so its unread bytes can be retried.
                progress.pop(fp, None)
            continue
    result: list[UsageEventPush] = []
    for dedup_key, main_event in events.items():
        result.append(main_event)
        iterations = winning_iterations.get(dedup_key)
        if iterations:
            result.extend(_build_advisor_events(main_event, iterations))
    return result


def _file_stamp(stat: os.stat_result) -> list[int]:
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


# The audit walk of each (account, transcript) for the life of the process.
_AUDIT_PROGRESS: dict[tuple[str, str], dict[str, Any]] = {}


def _same_cli_file(cursor: dict[str, Any], stat: os.stat_result) -> bool:
    stamp = cursor.get("stamp")
    return isinstance(stamp, list) and stamp[:2] == [stat.st_dev, stat.st_ino]


def poll_anthropic_events(
    paths: list[Path],
    account_id: str,
    since: datetime,
    watermark: EventWatermark,
    *,
    audit_bytes: int = AUDIT_BYTES,
) -> list[UsageEventPush]:
    """Bootstrap at the account timestamp, then collect each transcript's new bytes."""
    ranges: dict[Path, tuple[int, int]] = {}
    floors: dict[Path, datetime] = {}
    stamps: dict[Path, list[int]] = {}
    baselines: dict[Path, datetime] = {}
    oldest = datetime.min.replace(tzinfo=UTC)
    prior: dict[Path, tuple[dict[str, Any], list[str | None]]] = {}
    idle: dict[str, dict[str, Any]] = {}
    # The fingerprint below covers only the head and the tail of a consumed
    # region. A rolling chunk audit re-reads a bounded slice of every
    # transcript's consumed region per scan (#1407): a changed chunk forces a
    # replay from the transcript's baseline. An idle transcript's walk lives in
    # _AUDIT_PROGRESS, and its cursor is staged only when the audit filled in
    # a missing digest, so an idle sidecar does not rewrite the watermark
    # file every cycle. The walk reaches the file with the next cursor
    # written for a transcript; a restart resumes from that point.
    resolved: list[Path] = []
    for path in paths:
        try:
            resolved.append(path.resolve())
        except (OSError, RuntimeError):
            continue
    resolved = list(dict.fromkeys(resolved))
    audited: dict[str, dict[str, Any]] = {}
    tracked: dict[str, os.stat_result] = {}
    for path in resolved:
        try:
            stat = path.stat()
        except OSError:
            continue
        cursor = watermark.anthropic_transcript(path, account_id)
        if cursor:
            walk = _AUDIT_PROGRESS.get((account_id, str(path)))
            if walk and walk["offset"] == cursor.get("offset"):
                cursor = {**cursor, **walk["fields"], "chunks": walk["chunks"]}
            audited[str(path)] = cursor
            tracked[str(path)] = stat
    stored = {key: cursor.get("chunks") for key, cursor in audited.items()}
    updated, dropped = audit(audited, tracked, audit_bytes, _same_cli_file)
    for key, cursor in updated.items():
        _AUDIT_PROGRESS[(account_id, key)] = {
            "offset": cursor["offset"],
            "chunks": cursor["chunks"],
            "fields": {k: cursor[k] for k in AUDIT_FIELDS if k in cursor},
        }
    for key in dropped:
        _AUDIT_PROGRESS.pop((account_id, key), None)
    for path in resolved:
        try:
            if path in ranges:
                continue
            stamp = _file_stamp(path.stat())
            cursor = (
                updated.get(str(path))
                or audited.get(str(path))
                or watermark.anthropic_transcript(path, account_id)
            )
            previous_stamp = cursor.get("stamp")
            offset = cursor.get("offset", 0)
            same_stamp = previous_stamp == stamp
            grew = (
                isinstance(previous_stamp, list)
                and len(previous_stamp) == len(stamp)
                and previous_stamp[:2] == stamp[:2]
                and stamp[2] > previous_stamp[2]
            )
            resume = (
                (same_stamp or grew)
                and str(path) not in dropped
                and isinstance(offset, int)
                and 0 <= offset <= stamp[2]
                and cursor.get("fingerprint") == transcript_fingerprint(path, offset)
            )
            # Same-size in-place writes can share a filesystem timestamp tick.
            # An incomplete tail still needs to be read, even if its prefix matches.
            if same_stamp and resume and offset == stamp[2]:
                if str(path) in updated and updated[str(path)]["chunks"] != stored[str(path)]:
                    idle[str(path)] = updated[str(path)]
                continue
            baseline = since
            if isinstance(cursor.get("baseline"), str):
                try:
                    baseline = datetime.fromisoformat(cursor["baseline"])
                except ValueError:
                    # An invalid saved baseline falls back to the account watermark.
                    pass
            ranges[path] = (offset if resume else 0, stamp[2])
            prior[path] = (
                (
                    {k: cursor[k] for k in AUDIT_FIELDS if k in cursor},
                    chunks_of(cursor, offset),
                )
                if resume
                else ({}, [])
            )
            floors[path] = oldest if resume and offset > 0 else baseline
            baselines[path] = baseline
            stamps[path] = stamp
        except (OSError, RuntimeError):
            continue

    progress = {path: start for path, (start, _) in ranges.items()}
    events = parse_anthropic_events(
        list(ranges), account_id, since, ranges=ranges, progress=progress, floors=floors
    )
    cursors: dict[str, dict[str, Any]] = dict(idle)
    for path, end in progress.items():
        stamp = stamps[path]
        fields, chunks = prior[path]
        try:
            fingerprint = transcript_fingerprint(path, end)
            chunks = extend_chunks(path, chunks, ranges[path][0], end)
            unchanged = _file_stamp(path.stat()) == stamp
        except OSError:
            unchanged = False
        # A write during parsing can invalidate what we read. Retry from the
        # original floor in that case, even if the account watermark advances.
        cursors[str(path)] = {
            "account": account_id,
            "baseline": baselines[path].isoformat(),
            "stamp": stamp if unchanged else None,
            "offset": end if unchanged else 0,
            "fingerprint": fingerprint if unchanged else None,
            **({"chunks": chunks, **fields} if unchanged else {}),
        }
    watermark.stage_anthropic_transcripts(cursors)
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
