"""MiniMax Code ledger reads: sessions, messages, attribution, and model ids.

Split out of ``minimax_code.py``. Nothing here advances the cursor. The parser
stays next to the pending-cursor registry and calls these helpers through the
``minimax_code`` module, so a patch of ``minimax_code._MAX_ROWS_PER_CYCLE``
still changes what the parser scans.

This is not an extractor, so it stays out of ``event_extractors``: that
package is one module per provider, and each of those modules stamps a
``source_app``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.minimax_code_schema import (
    _MESSAGE_ROWS_TABLE,
    _OPTIONAL_SESSION_COLUMNS,
    _PROVIDER_MODEL_PREFIX,
    _REQUIRED_COLUMNS,
    _SESSIONS_TABLE,
    _TOKEN_USAGE_TABLE,
    SUBAGENT_TYPE_TASK,
)
from scripts.sidecar_pkg.sqlite_cursor import iter_cursor


def _chunks[T](items: list[T], size: int) -> list[list[T]]:
    """Split a list into chunks of at most ``size`` entries."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _schema_ok(cur: sqlite3.Cursor) -> bool:
    """True when every required table and column is present."""
    for table, columns in _REQUIRED_COLUMNS.items():
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            return False
        present = {info[1] for info in cur.execute(f"PRAGMA table_info({table})")}
        if not set(columns).issubset(present):
            return False
    return True


def _max_row_id(cur: sqlite3.Cursor) -> int:
    """Return the highest ledger id in the store (0 when it is empty)."""
    row = cur.execute(f"SELECT MAX(id) FROM {_TOKEN_USAGE_TABLE}").fetchone()
    return int(row[0] or 0) if row else 0


def _bootstrap_last_id(cur: sqlite3.Cursor, since: datetime) -> int:
    """Return the id to start from on a first run.

    The shared timestamp watermark's bootstrap window decides *which* rows
    are interesting, but it is applied on the **id** axis — the lowest id
    whose ``ts`` falls inside the window. Filtering the scan itself by
    ``ts`` would drop the interleaved rows that carry an older timestamp
    (see the module docstring), so the window is turned into an id
    boundary once and then forgotten.
    """
    since_ms = int(since.timestamp() * 1000)
    row = cur.execute(
        f"SELECT MIN(id) FROM {_TOKEN_USAGE_TABLE} WHERE ts > ?", (since_ms,)
    ).fetchone()
    if row is None or row[0] is None:
        # Nothing inside the window — skip to the end of the table so the
        # next cycle does not rescan it.
        return _max_row_id(cur) + 1
    return int(row[0])


def _table_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    """Return the columns a table actually has in this store."""
    return {info[1] for info in cur.execute(f"PRAGMA table_info({table})")}


def _load_sessions(cur: sqlite3.Cursor, session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return the session metadata (cwd, effective model, history dir, parent link).

    The parent-link columns are optional: a store written by a runtime that
    predates them still loads — it simply has no parent to file the rows
    under, which is today's behaviour rather than a skipped source.
    """
    if not session_ids:
        return {}
    present = _table_columns(cur, _SESSIONS_TABLE)
    optional = [name for name in _OPTIONAL_SESSION_COLUMNS if name in present]
    select = ", ".join(
        ["session_id", "workspace_dir", "extra_data_json", "history_relative_dir", *optional]
    )
    sessions: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(session_ids, 200):
        placeholders = ",".join("?" for _ in chunk)
        query = f"SELECT {select} FROM {_SESSIONS_TABLE} WHERE session_id IN ({placeholders})"
        for row in iter_cursor(cur.execute(query, chunk)):
            extra: dict[str, Any] = {}
            try:
                parsed = json.loads(row["extra_data_json"] or "{}")
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                extra = parsed
            entry: dict[str, Any] = {
                "workspace_dir": row["workspace_dir"],
                "history_relative_dir": row["history_relative_dir"],
                "extra": extra,
            }
            for name in optional:
                entry[name] = row[name]
            sessions[row["session_id"]] = entry
    return sessions


def _agent_label(agent_name: str | None) -> str:
    """Return the dashboard label of a runtime agent name.

    The runtime spells its agents in lower case (``explore``); Claude Code
    spells its built-in agent ``Explore``, and the Sessions view groups the
    subagent cards by this label.
    """
    name = (agent_name or "").strip()
    if not name:
        return SUBAGENT_TYPE_TASK
    return name[:1].upper() + name[1:]


def _child_attribution(
    session: dict[str, Any], sessions: dict[str, dict[str, Any]]
) -> dict[str, str | None] | None:
    """Return the attribution of a background-task session, or None for a main session.

    A child session is one the runtime spawned for a background task: it
    carries a ``parent_session_id``. Its rows are filed under that parent and
    tagged with the agent's name, so the Sessions view shows the work as the
    parent's subagent traffic — the same shape a Claude Code sidechain has.

    A parent that is not in this store is **not** invented: the rows keep
    their own session id and are only tagged, so the work stays labelled as
    subagent traffic instead of landing on a session that does not exist.
    """
    parent_id = session.get("parent_session_id")
    if not parent_id:
        return None
    return {"parent_session_id": parent_id if parent_id in sessions else None}


def _all_session_ids(cur: sqlite3.Cursor) -> list[str]:
    """Return every session id in the store."""
    rows = cur.execute(f"SELECT session_id FROM {_SESSIONS_TABLE}").fetchall()
    return [str(row[0]) for row in rows if row and row[0]]


def background_session_links(cur: sqlite3.Cursor) -> dict[str, dict[str, Any]]:
    """Return every background-task session of the store and its attribution.

    A background task is a session the runtime spawned: it carries a
    ``parent_session_id`` pointing at the session that owns the work. The
    rows of such a session are filed under that parent and tagged with the
    agent's name.

    The mapping is keyed by the *child* session id and holds the parent to
    file the rows under, the label to tag them with and the provenance to
    record with them. A parent that is not in the store is not invented: the
    task keeps its own session id and is only tagged.

    The repair script (``scripts/link_minimax_background_sessions.py``) uses
    this to build the mapping it applies to the rows that were collected
    before the extractor learned the parent link.
    """
    cur.row_factory = sqlite3.Row
    sessions = _load_sessions(cur, _all_session_ids(cur))
    children = {sid: entry for sid, entry in sessions.items() if entry.get("parent_session_id")}
    if not children:
        return {}

    # A session row without an agent name falls back to the ledger's own
    # per-row agent name — the same fallback the extractor applies.
    ledger_agents: dict[str, str] = {}
    if any(not entry.get("agent_name") for entry in children.values()):
        if "agent_name" in _table_columns(cur, _TOKEN_USAGE_TABLE):
            placeholders = ",".join("?" for _ in children)
            rows = cur.execute(
                f"SELECT session_id, agent_name FROM {_TOKEN_USAGE_TABLE} "
                f"WHERE session_id IN ({placeholders})",
                list(children),
            ).fetchall()
            for row in rows:
                if row and row[1]:
                    ledger_agents.setdefault(str(row[0]), str(row[1]))

    links: dict[str, dict[str, Any]] = {}
    for sid, entry in children.items():
        attribution = _child_attribution(entry, sessions)
        if attribution is None:  # pragma: no cover - the filter above guarantees it
            continue
        agent_name = entry.get("agent_name") or ledger_agents.get(sid)
        links[sid] = {
            "parent_session_id": attribution["parent_session_id"],
            "label": _agent_label(agent_name),
            "agent_name": agent_name,
            "purpose": entry.get("purpose"),
        }
    return links


def _index_message_row(
    index: dict[tuple[Any, ...], list[tuple[int, dict[str, Any]]]],
    row: sqlite3.Row,
) -> None:
    """Add one message row to the usage-tuple index."""
    try:
        data = json.loads(row["data_json"] or "{}")
    except (TypeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return
    key = (
        row["session_id"],
        row["turn_id"],
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("cache_read"),
    )
    index.setdefault(key, []).append((int(row["id"]), data))


def _load_message_index(
    cur: sqlite3.Cursor,
    turn_pairs: set[tuple[str | None, str | None]],
) -> dict[tuple[Any, ...], list[tuple[int, dict[str, Any]]]]:
    """Index the message rows of the pending window by their usage tuple.

    The join key is ``(session_id, turn_id, input, output, cache_read)``.
    It matches the ledger's per-request values against the message's own
    ``usage`` block — the two agree value for value, which is what makes
    the message row the right twin of a request.

    Only the turns of the pending window are read: the store keeps every
    message of every session, and parsing all of them would cost more than
    the rest of the extraction put together.
    """
    index: dict[tuple[Any, ...], list[tuple[int, dict[str, Any]]]] = {}
    pairs = sorted((session, turn) for session, turn in turn_pairs if session and turn)
    # A NULL turn never matches a row-value comparison, so those sessions
    # are read with their own predicate.
    null_turn_sessions = sorted({session for session, turn in turn_pairs if session and not turn})

    for chunk in _chunks(pairs, 200):
        placeholders = ",".join("(?,?)" for _ in chunk)
        params = [value for pair in chunk for value in pair]
        query = (
            f"SELECT id, session_id, turn_id, data_json FROM {_MESSAGE_ROWS_TABLE} "
            f"WHERE (session_id, turn_id) IN (VALUES {placeholders})"
        )
        for row in iter_cursor(cur.execute(query, params)):
            _index_message_row(index, row)

    if null_turn_sessions:
        placeholders = ",".join("?" for _ in null_turn_sessions)
        query = (
            f"SELECT id, session_id, turn_id, data_json FROM {_MESSAGE_ROWS_TABLE} "
            f"WHERE turn_id IS NULL AND session_id IN ({placeholders})"
        )
        for row in iter_cursor(cur.execute(query, null_turn_sessions)):
            _index_message_row(index, row)

    # Lowest id first: a stable choice when two message rows share a usage
    # tuple, so the enrichment never flips between two runs.
    for candidates in index.values():
        candidates.sort(key=lambda item: item[0])
    return index


def background_provenance(
    source_session_id: str,
    agent_name: str | None,
    parent_session_id: str | None,
    purpose: str | None,
) -> str:
    """Return the provenance recorded with a background-task row.

    It names the session the work really came from, so the child session
    stays recoverable from the stored row even though the row is filed under
    the parent. The blob is kept to four short keys — it is materialized
    whenever a wide window is scanned.

    The repair script writes the same blob, through this helper.
    """
    provenance: dict[str, str] = {"source_session_id": source_session_id}
    if agent_name:
        provenance["agent_name"] = agent_name
    if parent_session_id:
        provenance["parent_session_id"] = parent_session_id
    if purpose:
        provenance["purpose"] = purpose
    return json.dumps(provenance)


def build_event_id(
    session_id: str,
    turn_id: str,
    ts_ms: int,
    tokens_input: int,
    tokens_output: int,
    tokens_cache_read: int,
) -> str:
    """Return the event id of one ledger request.

    Derived from the **ledger row** alone — never from the session the event
    is filed under. A background-task row is filed under its *parent* session,
    but its id keeps the child's own session id: the id must not change when
    the attribution changes, or every row collected before the change would
    look new to the server and be counted twice.

    The repair script (``scripts/link_minimax_background_sessions.py``) builds
    the same ids with this helper, so the two cannot drift apart.
    """
    return (
        f"mcode:{session_id}:{turn_id}:{ts_ms}:{tokens_input}:{tokens_output}:{tokens_cache_read}"
    )


def _normalize_model(model: str) -> str | None:
    """Strip the provider prefix so the id matches the pricing seed key."""
    name = (model or "").strip()
    if not name:
        return None
    if name.lower().startswith(_PROVIDER_MODEL_PREFIX):
        name = name[len(_PROVIDER_MODEL_PREFIX) :]
    return name or None


def _sessions_root(db_path: Path) -> Path | None:
    """Return the runtime's session-history root for a given store path."""
    try:
        root = db_path.resolve().parents[2]
    except IndexError:
        return None
    return root / "v2" / "sessions"


def _model_from_history(
    session_id: str,
    session: dict[str, Any],
    db_path: Path,
    cache: dict[str, str | None],
) -> str | None:
    """Return the model named by the session's ``messages.jsonl``.

    Only a fallback: the history JSONL is pruned, so it is not a reliable
    source of *usage* — but it still names the model it talked to.
    """
    cache_key = f"history:{session_id}"
    if cache_key in cache:
        return cache[cache_key]
    model: str | None = None
    relative = session.get("history_relative_dir")
    root = _sessions_root(db_path)
    if relative and root:
        path = root / relative / "messages.jsonl"
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    message = entry.get("message") if isinstance(entry, dict) else None
                    if not isinstance(message, dict):
                        continue
                    candidate = message.get("model")
                    if isinstance(candidate, str) and candidate.strip():
                        model = candidate.strip()
                        break
        except OSError:
            model = None
    cache[cache_key] = model
    return model


def _model_from_llm_call(
    session_id: str,
    session: dict[str, Any],
    db_path: Path,
    cache: dict[str, str | None],
) -> str | None:
    """Return the model named by the session's ``llm-call.json``."""
    cache_key = f"llm-call:{session_id}"
    if cache_key in cache:
        return cache[cache_key]
    model: str | None = None
    relative = session.get("history_relative_dir")
    root = _sessions_root(db_path)
    if relative and root:
        path = root / relative / "llm-call.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            candidate = payload.get("model")
            if isinstance(candidate, str) and candidate.strip():
                model = candidate.strip()
    cache[cache_key] = model
    return model


def _resolve_model(
    session_id: str,
    data: dict[str, Any],
    session: dict[str, Any],
    db_path: Path,
    cache: dict[str, str | None],
) -> str | None:
    """Resolve the model of one request.

    First the message row's own telemetry, then the session's history
    JSONL, then the session's ``effectiveModel``, then ``llm-call.json``.
    The session-level fallbacks are always present for a live session, so
    a request whose message row has not landed yet still gets a model.
    """
    telemetry = data.get("context_usage_telemetry")
    if isinstance(telemetry, dict):
        candidate = telemetry.get("model")
        if isinstance(candidate, str) and candidate.strip():
            return _normalize_model(candidate)

    candidate = _model_from_history(session_id, session, db_path, cache)
    if candidate:
        return _normalize_model(candidate)

    extra = session.get("extra") or {}
    effective = extra.get("effectiveModel") if isinstance(extra, dict) else None
    if isinstance(effective, str) and effective.strip():
        return _normalize_model(effective)

    candidate = _model_from_llm_call(session_id, session, db_path, cache)
    if candidate:
        return _normalize_model(candidate)

    return None


def _tool_names(data: dict[str, Any]) -> list[str]:
    """Return the tool names the request's message row recorded."""
    calls = data.get("tool_calls")
    if not isinstance(calls, list):
        return []
    names: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("tool_name") or call.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _stop_reason(data: dict[str, Any]) -> str | None:
    """Return the message row's finish reason."""
    reason = data.get("finish_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return None


def _effort(session: dict[str, Any]) -> str | None:
    """Return the session's configured thinking effort."""
    extra = session.get("extra") or {}
    thinking = extra.get("effectiveModelThinking") if isinstance(extra, dict) else None
    if isinstance(thinking, dict):
        effort = thinking.get("effort")
        if isinstance(effort, str) and effort.strip():
            return effort.strip().lower()
    return None
