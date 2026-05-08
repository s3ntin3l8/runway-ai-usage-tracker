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
