"""Helper to create a MiniMax Code runtime store for testing.

The runtime keeps its usage ledger in a SQLite store under its app data
dir (``~/.minimax/v2/sqlite/runtime-state.sqlite``). The fixture mirrors
that layout so the extractor's history fallbacks — which resolve the
session's ``messages.jsonl`` and ``llm-call.json`` relative to the store —
work against the fixture the same way they work against a real install.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA = (
    """
    CREATE TABLE local_runtime_token_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        framework_type TEXT NOT NULL,
        turn_id TEXT,
        model TEXT,
        ts INTEGER NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        reasoning_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL,
        cache_write_tokens INTEGER NOT NULL,
        cost_usd REAL,
        raw TEXT
    )
    """,
    """
    CREATE TABLE local_runtime_message_rows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        msg_id TEXT NOT NULL,
        role TEXT,
        turn_id TEXT,
        created_at_ms INTEGER NOT NULL,
        data_json TEXT NOT NULL,
        source TEXT,
        source_context_json TEXT,
        UNIQUE(session_id, msg_id)
    )
    """,
)

# The session columns a later runtime migration added: the parent link of a
# background-task session, its kind and its purpose. A store without them is
# an older runtime — the extractor must degrade to "no attribution", never to
# "skipped source", so both shapes are buildable.
_PARENT_LINK_COLUMNS = ("parent_session_id", "session_kind", "purpose")


def _sessions_table(*, parent_links: bool) -> str:
    """Return the CREATE statement for the session table.

    ``parent_links=True`` adds the columns a background-task session needs.
    """
    extra = "".join(f", {name} TEXT" for name in _PARENT_LINK_COLUMNS) if parent_links else ""
    return f"""
    CREATE TABLE local_runtime_sessions (
        session_id TEXT PRIMARY KEY,
        record_json TEXT NOT NULL,
        updated_at_ms INTEGER NOT NULL,
        agent_name TEXT,
        workspace_dir TEXT,
        title TEXT,
        extra_data_json TEXT NOT NULL DEFAULT '{{}}',
        history_relative_dir TEXT{extra}
    )
    """


# 2026-10-22T20:12:31.617Z and neighbours.
_TS = 1792699951617

DEFAULT_SESSIONS: list[dict[str, Any]] = [
    {
        "session_id": "mvs_main",
        "workspace_dir": "/home/user/project",
        "history_relative_dir": "2026/10/22/20-28-11-224-session_main",
        "extra_data_json": {
            "effectiveModel": "minimax/MiniMax-M3.1-Flash",
            "effectiveModelThinking": {"effort": "xhigh"},
        },
    },
    {
        # No effectiveModel: the model has to come from the session history.
        "session_id": "mvs_other",
        "workspace_dir": "/home/user/other-project",
        "history_relative_dir": "2026/10/22/21-00-00-000-session_other",
        "extra_data_json": {},
    },
]

DEFAULT_USAGE: list[dict[str, Any]] = [
    {
        "id": 1,
        "session_id": "mvs_main",
        "turn_id": "turn_main",
        "ts": _TS,
        "input_tokens": 16474,
        "output_tokens": 203,
        "reasoning_tokens": 0,
        "cache_read_tokens": 1536,
        "cache_write_tokens": 0,
    },
    {
        "id": 2,
        "session_id": "mvs_main",
        "turn_id": "turn_main",
        "ts": _TS + 6306,
        "input_tokens": 2566,
        "output_tokens": 354,
        "reasoning_tokens": 0,
        "cache_read_tokens": 18176,
        "cache_write_tokens": 0,
    },
    {
        "id": 3,
        "session_id": "mvs_main",
        "turn_id": "mavis-internal:plan-review:ask_224136e4e51877bac4a8ffe8",
        "ts": _TS + 9000,
        "input_tokens": 800,
        "output_tokens": 120,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    },
    {
        # No message row yet — the request is still in flight. The model has
        # to fall back to the session's effectiveModel.
        "id": 4,
        "session_id": "mvs_main",
        "turn_id": "turn_main",
        "ts": _TS + 12000,
        "input_tokens": 500,
        "output_tokens": 60,
        "reasoning_tokens": 0,
        "cache_read_tokens": 100,
        "cache_write_tokens": 0,
    },
    {
        # A session without an effectiveModel and a message row without
        # telemetry: the model comes from the session's messages.jsonl.
        "id": 5,
        "session_id": "mvs_other",
        "turn_id": "turn_other",
        "ts": _TS + 20000,
        "input_tokens": 300,
        "output_tokens": 42,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    },
]

DEFAULT_MESSAGES: list[dict[str, Any]] = [
    {
        "id": 1,
        "session_id": "mvs_main",
        "turn_id": "turn_main",
        "msg_id": "msg_main_1",
        "role": "assistant",
        "created_at_ms": _TS + 400,
        "source": "api",
        "data_json": {
            "canonical_message_id": "msg-CYCOmain1",
            "turn_id": "turn_main",
            "usage": {
                "total_tokens": 18213,
                "context_window": 1000000,
                "input_tokens": 16474,
                "output_tokens": 203,
                "cache_read": 1536,
            },
            "context_usage_telemetry": {
                "model": "MiniMax-M3.1-Flash",
                "turnId": "turn_main",
            },
            "tool_calls": [
                {"tool_name": "bash", "tool_call_id": "call_1"},
                {"tool_name": "read", "tool_call_id": "call_2"},
            ],
            "finish_reason": "toolUse",
        },
    },
    {
        "id": 2,
        "session_id": "mvs_main",
        "turn_id": "turn_main",
        "msg_id": "msg_main_2",
        "role": "assistant",
        "created_at_ms": _TS + 6700,
        "source": "api",
        "data_json": {
            "canonical_message_id": "msg-CYCOmain2",
            "turn_id": "turn_main",
            "usage": {
                "total_tokens": 21096,
                "input_tokens": 2566,
                "output_tokens": 354,
                "cache_read": 18176,
            },
            "context_usage_telemetry": {
                "model": "MiniMax-M3.1-Flash",
                "turnId": "turn_main",
            },
            "tool_calls": [{"tool_name": "edit"}],
            "finish_reason": "toolUse",
        },
    },
    {
        "id": 3,
        "session_id": "mvs_main",
        "turn_id": "mavis-internal:plan-review:ask_224136e4e51877bac4a8ffe8",
        "msg_id": "msg_plan_1",
        "role": "assistant",
        "created_at_ms": _TS + 9400,
        "source": "questionnaire",
        "data_json": {
            "canonical_message_id": "msg-CYCOplan1",
            "turn_id": "mavis-internal:plan-review:ask_224136e4e51877bac4a8ffe8",
            "usage": {
                "total_tokens": 920,
                "input_tokens": 800,
                "output_tokens": 120,
                "cache_read": 0,
            },
            "context_usage_telemetry": {
                "model": "MiniMax-M3.1-Flash",
                "turnId": "mavis-internal:plan-review:ask_224136e4e51877bac4a8ffe8",
            },
            "tool_calls": [],
            "finish_reason": "end_turn",
        },
    },
    {
        # No telemetry block: the model has to come from the session history.
        "id": 4,
        "session_id": "mvs_other",
        "turn_id": "turn_other",
        "msg_id": "msg_other_1",
        "role": "assistant",
        "created_at_ms": _TS + 20400,
        "source": "api",
        "data_json": {
            "canonical_message_id": "msg-CYCOother1",
            "turn_id": "turn_other",
            "usage": {
                "total_tokens": 342,
                "input_tokens": 300,
                "output_tokens": 42,
                "cache_read": 0,
            },
            "tool_calls": [{"tool_name": "grep"}],
            "finish_reason": "end_turn",
        },
    },
]


def make_minimax_code_store(
    root: Path,
    *,
    sessions: list[dict[str, Any]] | None = None,
    usage: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    data_dir: str = ".minimax",
    parent_links: bool = False,
) -> Path:
    """Create a MiniMax Code runtime store and return its path.

    The store lands at ``<root>/<data_dir>/v2/sqlite/runtime-state.sqlite``
    so the extractor resolves the session history the same way it does on a
    real install. ``data_dir`` defaults to the runtime's own ``.minimax``;
    the desktop app's app-data roots are covered by the probe's tests.

    ``parent_links=True`` gives the session table the columns a
    background-task session needs (``parent_session_id``, ``session_kind``,
    ``purpose``). The default store keeps the older shape, so a test can
    cover the runtime that predates them.
    """
    db_dir = root / data_dir / "v2" / "sqlite"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "runtime-state.sqlite"

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        for statement in _SCHEMA:
            cur.execute(statement)
        cur.execute(_sessions_table(parent_links=parent_links))

        session_columns = [
            "session_id",
            "record_json",
            "updated_at_ms",
            "agent_name",
            "workspace_dir",
            "title",
            "extra_data_json",
            "history_relative_dir",
        ]
        if parent_links:
            session_columns.extend(_PARENT_LINK_COLUMNS)
        placeholders = ",".join("?" for _ in session_columns)

        for session in sessions if sessions is not None else DEFAULT_SESSIONS:
            extra = session.get("extra_data_json")
            if not isinstance(extra, str):
                extra = json.dumps(extra or {})
            values: list[Any] = [
                session["session_id"],
                "{}",
                session.get("updated_at_ms", _TS),
                session.get("agent_name", "mavis"),
                session.get("workspace_dir"),
                session.get("title"),
                extra,
                session.get("history_relative_dir"),
            ]
            if parent_links:
                values.extend(
                    [
                        session.get("parent_session_id"),
                        session.get("session_kind"),
                        session.get("purpose"),
                    ]
                )
            cur.execute(
                f"INSERT INTO local_runtime_sessions ({', '.join(session_columns)}) "
                f"VALUES ({placeholders})",
                values,
            )

        for row in usage if usage is not None else DEFAULT_USAGE:
            raw = {
                "input": row["input_tokens"],
                "output": row["output_tokens"],
                "cacheRead": row["cache_read_tokens"],
                "cacheWrite": row["cache_write_tokens"],
                "totalTokens": (
                    row["input_tokens"]
                    + row["output_tokens"]
                    + row["cache_read_tokens"]
                    + row["cache_write_tokens"]
                ),
            }
            cur.execute(
                "INSERT INTO local_runtime_token_usage "
                "(id, session_id, agent_name, framework_type, turn_id, model, ts, "
                " input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, "
                " cache_write_tokens, cost_usd, raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["session_id"],
                    row.get("agent_name", "mavis"),
                    row.get("framework_type", "pi-agent"),
                    row.get("turn_id"),
                    row.get("model"),
                    row["ts"],
                    row["input_tokens"],
                    row["output_tokens"],
                    row.get("reasoning_tokens", 0),
                    row["cache_read_tokens"],
                    row.get("cache_write_tokens", 0),
                    # The M Plan is a subscription: the runtime always writes 0.
                    row.get("cost_usd", 0.0),
                    json.dumps(raw),
                ),
            )

        for row in messages if messages is not None else DEFAULT_MESSAGES:
            data = row["data_json"]
            if not isinstance(data, str):
                data = json.dumps(data)
            cur.execute(
                "INSERT INTO local_runtime_message_rows "
                "(id, session_id, msg_id, role, turn_id, created_at_ms, data_json, source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["session_id"],
                    row["msg_id"],
                    row.get("role", "assistant"),
                    row.get("turn_id"),
                    row.get("created_at_ms", _TS),
                    data,
                    row.get("source", "api"),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def write_session_history(
    root: Path,
    relative_dir: str,
    *,
    model: str | None = None,
    llm_model: str | None = None,
) -> Path:
    """Write a session's ``messages.jsonl`` / ``llm-call.json`` under ``root``.

    Returns the session directory. ``model`` names the model in the
    history JSONL, ``llm_model`` the one in ``llm-call.json`` — the two
    are separate fallbacks, so a test can leave one of them out.
    """
    session_dir = root / ".minimax" / "v2" / "sessions" / relative_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    if model:
        entries = [
            {
                "message_id": "msg-history-1",
                "turn_id": "turn_history",
                "message": {
                    "role": "user",
                    "content": "hello",
                },
            },
            {
                "message_id": "msg-history-2",
                "turn_id": "turn_history",
                "message": {
                    "role": "assistant",
                    "model": model,
                    "provider": "minimax",
                    "usage": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
                },
            },
        ]
        (session_dir / "messages.jsonl").write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n",
            encoding="utf-8",
        )
    if llm_model:
        (session_dir / "llm-call.json").write_text(
            json.dumps({"api": "anthropic-messages", "provider": "minimax", "model": llm_model}),
            encoding="utf-8",
        )
    return session_dir
