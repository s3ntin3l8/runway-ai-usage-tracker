"""Session names from Codex and agy: fixture databases, no real home dirs."""

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from scripts import sidecar
from scripts.sidecar_pkg import session_names
from scripts.sidecar_pkg.event_extractors.antigravity import parse_antigravity_session_names
from scripts.sidecar_pkg.event_extractors.chatgpt import parse_chatgpt_session_names

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
UUID_C = "33333333-3333-4333-8333-333333333333"
LONG_PROMPT = "FIRST-PROMPT-MARKER please do\nthis very long thing\n" + "x" * 3000


@pytest.fixture(autouse=True)
def _clean_state():
    session_names.clear_state()
    yield
    session_names.clear_state()


def _codex_home(tmp_path: Path) -> tuple[Path, list[Path]]:
    home = tmp_path / "codex"
    day = home / "sessions" / "2026" / "10" / "30"
    day.mkdir(parents=True)
    rollouts = []
    for uuid in (UUID_A, UUID_B, UUID_C):
        path = day / f"rollout-2026-10-30T10-00-00-{uuid}.jsonl"
        path.write_text("{}\n")
        rollouts.append(path)
    conn = sqlite3.connect(home / "state_5.sqlite")
    conn.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, preview TEXT, "
        "first_user_message TEXT, rollout_path TEXT, agent_nickname TEXT)"
    )
    conn.executemany(
        "INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
        [
            # Named; the rollout path says where the events came from.
            (
                UUID_A,
                "Fix the parser",
                LONG_PROMPT,
                LONG_PROMPT,
                LONG_PROMPT,
                str(rollouts[0]),
                "Nick",
            ),
            # Unnamed: the prompt columns must never become a name.
            (UUID_B, None, LONG_PROMPT, LONG_PROMPT, LONG_PROMPT, str(rollouts[1]), None),
            # Named only through the session index; no rollout_path.
            (UUID_C, "", LONG_PROMPT, LONG_PROMPT, LONG_PROMPT, None, None),
        ],
    )
    conn.commit()
    conn.close()
    (home / "session_index.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {"id": UUID_C, "thread_name": "Index only name", "updated_at": "x"},
                {"id": UUID_B, "thread_name": "", "updated_at": "x"},
            )
        )
    )
    return home, rollouts


def test_codex_names(tmp_path):
    home, rollouts = _codex_home(tmp_path)
    records = parse_chatgpt_session_names([home], rollouts)
    got = {r["session_id"]: r["name"] for r in records}
    assert got == {
        rollouts[0].stem: "Fix the parser",
        rollouts[2].stem: "Index only name",
    }
    assert all(
        r["provider_id"] == "chatgpt" and r["kind"] == "agent" and r["source_app"] == "codex"
        for r in records
    )
    assert "FIRST-PROMPT-MARKER" not in json.dumps(records)


def test_codex_rollout_path_wins_and_unknown_thread_is_skipped(tmp_path):
    home, _ = _codex_home(tmp_path)
    # No discovered rollout files: only the thread with a rollout_path is keyed.
    records = parse_chatgpt_session_names([home], [])
    assert [r["session_id"] for r in records] == [f"rollout-2026-10-30T10-00-00-{UUID_A}"]


def test_codex_state_db_without_rollout_column(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    conn = sqlite3.connect(home / "state_5.sqlite")
    conn.execute("CREATE TABLE threads (id TEXT, name TEXT)")
    conn.execute("INSERT INTO threads VALUES (?, ?)", (UUID_A, "Named"))
    conn.commit()
    conn.close()
    rollout = tmp_path / f"rollout-2026-01-01T00-00-00-{UUID_A}.jsonl"
    records = parse_chatgpt_session_names([home], [rollout])
    assert [r["session_id"] for r in records] == [rollout.stem]


def _agy_db(tmp_path: Path) -> Path:
    db = tmp_path / "conversation_summaries.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, preview TEXT, "
        "agent_name TEXT, parent_conversation_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO conversation_summaries VALUES (?,?,?,?,?)",
        [
            ("parent-titled", "Parent Title Here", "PREVIEW-MARKER text", "", ""),
            ("parent-untitled", "", "PREVIEW-MARKER text", "", ""),
            ("child-titled", "Child Title Here", "PREVIEW-MARKER text", "scout", "parent-titled"),
            ("child-untitled", None, "PREVIEW-MARKER text", "scout", "parent-titled"),
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_agy_titles(tmp_path):
    records = parse_antigravity_session_names(_agy_db(tmp_path))
    assert {r["session_id"]: r["name"] for r in records} == {
        "parent-titled": "Parent Title Here",
        "child-titled": "Child Title Here",
    }
    assert all(
        r["provider_id"] == "antigravity"
        and r["kind"] == "generated"
        and r["source_app"] == "antigravity"
        for r in records
    )
    assert "PREVIEW-MARKER" not in json.dumps(records)


def test_collectors_registered_and_unchanged_rerun_sends_nothing(tmp_path, monkeypatch, caplog):
    home, rollouts = _codex_home(tmp_path)
    monkeypatch.setattr(sidecar, "custom_login_dirs", lambda _env: [home])
    monkeypatch.setattr(sidecar, "_discover_codex_log_paths", lambda: rollouts)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    agy_dir = tmp_path / "nohome" / ".gemini" / "antigravity-cli"
    agy_dir.mkdir(parents=True)
    _agy_db(agy_dir).replace(agy_dir / "conversation_summaries.db")
    session_names.configure(tmp_path / "state.json")

    def cycle() -> list[dict]:
        out: list[dict] = []
        for provider in ("chatgpt", "antigravity"):
            sidecar._collect_session_names_for_provider(provider, out)
        return session_names.select_for_push(out)

    with caplog.at_level(logging.DEBUG):
        first = cycle()
        assert len(first) == 4
        session_names.commit(first)
        assert cycle() == []
    for secret in ("Fix the parser", "Index only name", "Parent Title Here", "Child Title Here"):
        assert secret not in caplog.text


def test_collector_failure_logs_type_only(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sidecar, "custom_login_dirs", lambda _env: [])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    broken = tmp_path / ".gemini" / "antigravity-cli"
    broken.mkdir(parents=True)
    (broken / "conversation_summaries.db").write_text("Secret Title not a database")
    with caplog.at_level(logging.DEBUG):
        sidecar._collect_session_names_for_provider("antigravity", [])
    assert "DatabaseError" in caplog.text
    assert "Secret Title" not in caplog.text


def test_newest_state_db_wins_numerically_and_bad_rollout_type_is_local(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    for version, name, rollout in (
        (9, "Old name", f"rollout-old-{UUID_A}.jsonl"),
        (10, "New name", f"rollout-new-{UUID_A}.jsonl"),
    ):
        conn = sqlite3.connect(home / f"state_{version}.sqlite")
        conn.execute("CREATE TABLE threads (id TEXT, name TEXT, rollout_path)")
        conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (UUID_A, name, rollout))
        conn.execute("INSERT INTO threads VALUES (?, ?, ?)", (UUID_B, "Odd", b"\x00blob"))
        conn.commit()
        conn.close()
    (home / "state_backup.sqlite").write_bytes(b"not a database")
    records = parse_chatgpt_session_names([home], [])
    assert {(r["session_id"], r["name"]) for r in records} == {
        (f"rollout-new-{UUID_A}", "New name")
    }
