"""Helper to create an in-memory OpenCode SQLite database for testing."""

import json
import sqlite3


def make_opencode_db(path: str = ":memory:") -> sqlite3.Connection:
    """Create an OpenCode-shaped SQLite DB with sample assistant messages."""
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS account (
            id TEXT PRIMARY KEY,
            email TEXT
        )
    """)
    # Seed account
    cur.execute("INSERT INTO account VALUES ('acc1', 'user@opencode.test')")

    messages = [
        {
            "id": "msg_opencode_001",
            "session_id": "ses_session_abc",
            "time_created": 1778248860000,  # 2026-05-08T14:01:00Z in ms
            "data": {
                "parentID": "msg_user_001",
                "role": "assistant",
                "mode": "normal",
                "agent": "coder",
                "path": {"cwd": "/home/user/project", "root": "/home/user/project"},
                "cost": 0.0042,
                "tokens": {
                    "total": 1500,
                    "input": 1200,
                    "output": 300,
                    "reasoning": 0,
                    "cache": {"write": 0, "read": 500},
                },
                "modelID": "claude-3-5-sonnet",
                "providerID": "opencode-go",
                "time": {"created": 1746709260000, "completed": 1746709262000},
                "finish": "end_turn",
                "variant": "high",
            },
        },
        {
            "id": "msg_opencode_002",
            "session_id": "ses_session_abc",
            "time_created": 1778248980000,  # 2026-05-08T14:03:00Z in ms
            "data": {
                "parentID": "msg_user_002",
                "role": "assistant",
                "mode": "normal",
                "agent": "coder",
                "path": {"cwd": "/home/user/project", "root": "/home/user/project"},
                "cost": 0.0088,
                "tokens": {
                    "total": 3200,
                    "input": 2500,
                    "output": 700,
                    "reasoning": 150,
                    "cache": {"write": 0, "read": 1200},
                },
                "modelID": "gpt-4o",
                "providerID": "opencode-go",
                "time": {"created": 1746709380000, "completed": 1746709383500},
                "finish": "end_turn",
                "variant": "medium",
            },
        },
        {
            "id": "msg_opencode_003",
            "session_id": "ses_session_xyz",
            "time_created": 1778249100000,  # 2026-05-08T14:05:00Z in ms
            "data": {
                "parentID": "msg_user_003",
                "role": "user",  # not assistant — should be ignored
                "mode": "normal",
                "agent": "coder",
                "path": {"cwd": "/home/user/project", "root": "/home/user/project"},
                "cost": 0.0,
                "tokens": {
                    "total": 0,
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache": {"write": 0, "read": 0},
                },
                "modelID": "claude-3-5-sonnet",
                "providerID": "opencode-go",
            },
        },
    ]

    for msg in messages:
        cur.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?)",
            (
                msg["id"],
                msg["session_id"],
                msg["time_created"],
                msg["time_created"],
                json.dumps(msg["data"]),
            ),
        )
    conn.commit()
    return conn


def make_opencode_v2_db(path: str, messages: list[dict], sessions: list[dict] | None = None):
    """Create an opencode v2-shaped DB (migrated in place, v1 tables left behind).

    Mirrors a real migrated ``opencode.db``: ``session_v2`` + ``session_message``
    (``type`` column, model nested in ``data``) alongside a *frozen* v1 ``message``
    table holding one stale assistant row that a v2 reader must ignore. Each
    message is ``{id, session_id, time_created, type, data}``; each session
    ``{id, directory, model}``.
    """
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    cur.execute(
        "INSERT INTO message VALUES ('msg_frozen_v1','ses_old',1,1,?)",
        (json.dumps({"role": "assistant", "modelID": "stale", "providerID": "opencode-go"}),),
    )
    cur.execute("CREATE TABLE account (id TEXT PRIMARY KEY, email TEXT)")
    cur.execute("INSERT INTO account VALUES ('acc1', 'user@opencode.test')")
    cur.execute(
        "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, model TEXT, title TEXT)"
    )
    cur.execute(
        "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
        "seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    for s in sessions or []:
        cur.execute(
            "INSERT INTO session_v2 VALUES (?,?,?,?)",
            (
                s["id"],
                s.get("directory"),
                json.dumps(s["model"]) if s.get("model") else None,
                s.get("title"),
            ),
        )
    for seq, m in enumerate(messages):
        cur.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (
                m["id"],
                m["session_id"],
                m["type"],
                seq,
                m["time_created"],
                m["time_created"],
                json.dumps(m["data"]),
            ),
        )
    conn.commit()
    conn.close()


def v2_assistant_data(created_ms: int, **overrides) -> dict:
    """A settled v2 assistant ``data`` payload (shape taken from a real row)."""
    data = {
        "agent": "build",
        "model": {"providerID": "xai", "id": "grok-4.3", "variant": "default"},
        "finish": "stop",
        "cost": 0.0409734,
        "tokens": {
            "input": 32476,
            "output": 1,
            "reasoning": 135,
            "cache": {"read": 192, "write": 0},
        },
        "time": {"created": created_ms, "completed": created_ms + 4924},
    }
    data.update(overrides)
    return data
