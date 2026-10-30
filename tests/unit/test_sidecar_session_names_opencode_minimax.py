"""Session titles from opencode and MiniMax Code (fake titles and ids only)."""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import sidecar
from scripts.sidecar_pkg import session_names
from scripts.sidecar_pkg.event_extractors.minimax_code import (
    parse_minimax_code_events,
    parse_minimax_code_session_names,
)
from scripts.sidecar_pkg.event_extractors.opencode import (
    parse_opencode_events,
    parse_opencode_session_names,
)
from tests.fixtures.minimax_code_fixture import make_minimax_code_store
from tests.fixtures.opencode_fixture import (
    make_opencode_db,
    make_opencode_v2_db,
    v2_assistant_data,
)

_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_state():
    session_names.clear_state()
    yield
    session_names.clear_state()


def _keys(records):
    return {
        (r["provider_id"], r["session_id"], r["kind"], r["source_app"]): r["name"] for r in records
    }


def _add_sessions(path: Path, table: str, rows: list[tuple[str, str | None]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, title TEXT, time_updated INTEGER)")
    for index, (session_id, title) in enumerate(rows):
        conn.execute(f"INSERT INTO {table} VALUES (?,?,?)", (session_id, title, index))
    conn.commit()
    conn.close()


def _opencode_v1(tmp_path: Path) -> Path:
    path = tmp_path / "opencode.db"
    make_opencode_db(str(path)).close()
    _add_sessions(
        path,
        "session",
        [
            ("ses_session_abc", "  Fake generated title  "),
            ("ses_session_xyz", None),
            ("ses_no_messages", "Fake title without events"),
        ],
    )
    return path


def test_opencode_v1_titled_session_is_emitted_untitled_is_not(tmp_path):
    records = parse_opencode_session_names(_opencode_v1(tmp_path))
    assert _keys(records) == {
        ("opencode", "ses_session_abc", "generated", "opencode"): "Fake generated title"
    }


def test_opencode_provider_matches_the_events_provider(tmp_path):
    path = _opencode_v1(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO message VALUES ('msg_extra', 'ses_session_abc', 1778249000000, 1778249000000, ?)",
        (
            json.dumps(
                {"role": "assistant", "modelID": "vendor/m:free", "providerID": "openrouter"}
            ),
        ),
    )
    conn.commit()
    conn.close()
    event_providers = {
        e.provider_id
        for e in parse_opencode_events(path, "default", _EPOCH)
        if e.session_id == "ses_session_abc"
    }
    name_providers = {r["provider_id"] for r in parse_opencode_session_names(path)}
    assert len(event_providers) == 2
    assert name_providers == event_providers


def test_opencode_v2_reads_session_v2_titles_and_retags(tmp_path):
    path = tmp_path / "opencode.db"
    make_opencode_v2_db(
        str(path),
        messages=[
            {
                "id": "m1",
                "session_id": "ses_v2_a",
                "time_created": 1778248860000,
                "type": "assistant",
                "data": v2_assistant_data(1778248860000),
            },
            {
                "id": "m2",
                "session_id": "ses_v2_b",
                "time_created": 1778248870000,
                "type": "assistant",
                "data": v2_assistant_data(1778248870000),
            },
        ],
        sessions=[
            {"id": "ses_v2_a", "directory": "/x", "title": "Fake v2 title"},
            {"id": "ses_v2_b", "directory": "/x", "title": "   "},
        ],
    )
    # The frozen v1 table holds stale titles that must not resurface.
    _add_sessions(
        path, "session", [("ses_v2_a", "Fake stale title"), ("ses_v2_b", "Fake stale title two")]
    )
    events = parse_opencode_events(path, "default", _EPOCH)
    records = parse_opencode_session_names(path)
    assert [r["session_id"] for r in records] == ["ses_v2_a"]
    assert records[0]["provider_id"] == next(
        e.provider_id for e in events if e.session_id == "ses_v2_a"
    )
    assert records[0]["name"] == "Fake v2 title"


def test_opencode_without_a_session_table_yields_nothing(tmp_path):
    path = tmp_path / "opencode.db"
    make_opencode_db(str(path)).close()
    assert parse_opencode_session_names(path) == []
    assert parse_opencode_session_names(tmp_path / "missing.db") == []


def test_opencode_unreadable_store_is_not_staged_as_scanned(tmp_path, monkeypatch):
    path = _opencode_v1(tmp_path)
    monkeypatch.setattr(sidecar, "_discover_opencode_db_path", lambda: path)
    session_names.configure(tmp_path / "state.json")
    from scripts.sidecar_pkg.event_extractors import opencode

    monkeypatch.setattr(opencode, "parse_opencode_session_names", lambda _p: None)
    out: list[dict] = []
    sidecar._collect_opencode_session_names(out)
    session_names.commit([])
    monkeypatch.undo()
    monkeypatch.setattr(sidecar, "_discover_opencode_db_path", lambda: path)
    sidecar._collect_opencode_session_names(out)
    assert len(out) == 1


def test_minimax_unreadable_store_is_none(tmp_path):
    bad = tmp_path / "runtime-state.sqlite"
    bad.write_bytes(b"not a database" * 100)
    assert parse_minimax_code_session_names(bad) is None


def test_opencode_unchanged_rerun_sends_nothing(tmp_path):
    path = _opencode_v1(tmp_path)
    session_names.configure(tmp_path / "state.json")
    sent = session_names.select_for_push(parse_opencode_session_names(path))
    assert len(sent) == 1
    session_names.commit(sent)
    assert session_names.select_for_push(parse_opencode_session_names(path)) == []


_SESSIONS = [
    {"session_id": "mvs_titled", "title": "Fake minimax title", "agent_name": "mavis"},
    {"session_id": "mvs_blank", "title": "  ", "agent_name": "mavis"},
    {"session_id": "mvs_none", "title": None, "agent_name": "mavis"},
    {"session_id": "mvs_child", "title": "Fake child title", "parent_session_id": "mvs_titled"},
    {"session_id": "mvs_orphan", "title": "Fake orphan title", "parent_session_id": "mvs_gone"},
]


def test_minimax_emits_titles_not_agent_names_and_skips_filed_children(tmp_path):
    db = make_minimax_code_store(tmp_path, sessions=_SESSIONS, parent_links=True)
    assert _keys(parse_minimax_code_session_names(db)) == {
        ("minimax", "mvs_titled", "generated", "minimax_code"): "Fake minimax title",
        ("minimax", "mvs_orphan", "generated", "minimax_code"): "Fake orphan title",
    }


def test_minimax_store_without_parent_columns_still_works(tmp_path):
    db = make_minimax_code_store(tmp_path, sessions=_SESSIONS[:3])
    assert [r["session_id"] for r in parse_minimax_code_session_names(db)] == ["mvs_titled"]


def test_minimax_session_id_matches_the_events(tmp_path):
    db = make_minimax_code_store(
        tmp_path, sessions=[{"session_id": "mvs_main", "title": "Fake title"}]
    )
    events = parse_minimax_code_events(db, "default", _EPOCH, state_file=tmp_path / "cursor.json")
    record = parse_minimax_code_session_names(db)[0]
    assert {e.session_id for e in events if e.session_id == "mvs_main"}
    assert record["provider_id"] == events[0].provider_id
    assert record["source_app"] == events[0].source_app


def test_minimax_covers_every_profile_root(tmp_path, monkeypatch):
    first = make_minimax_code_store(
        tmp_path, sessions=[{"session_id": "mvs_one", "title": "Fake one"}]
    )
    second = make_minimax_code_store(
        tmp_path,
        sessions=[{"session_id": "mvs_two", "title": "Fake two"}],
        data_dir=".minimax-work",
    )
    monkeypatch.setattr(sidecar, "_discover_minimax_code_db_paths", lambda: [first, second])
    out: list[dict] = []
    sidecar._collect_minimax_code_session_names(out)
    assert {r["session_id"] for r in out} == {"mvs_one", "mvs_two"}
    assert (
        sidecar._SESSION_NAME_COLLECTORS["minimax"] is sidecar._collect_minimax_code_session_names
    )


def test_collectors_are_registered_and_skip_unchanged_inputs(tmp_path, monkeypatch):
    assert sidecar._SESSION_NAME_COLLECTORS["opencode"] is sidecar._collect_opencode_session_names
    assert (
        sidecar._SESSION_NAME_COLLECTORS["minimax_code"]
        is sidecar._collect_minimax_code_session_names
    )
    path = _opencode_v1(tmp_path)
    monkeypatch.setattr(sidecar, "_discover_opencode_db_path", lambda: path)
    session_names.configure(tmp_path / "state.json")
    first: list[dict] = []
    sidecar._collect_opencode_session_names(first)
    assert len(first) == 1
    session_names.commit(session_names.select_for_push(first))
    second: list[dict] = []
    sidecar._collect_opencode_session_names(second)
    assert second == []


def test_titles_are_never_logged(tmp_path, monkeypatch, caplog):
    path = _opencode_v1(tmp_path)
    store = make_minimax_code_store(
        tmp_path, sessions=[{"session_id": "mvs_x", "title": "Fake secret title"}]
    )
    monkeypatch.setattr(sidecar, "_discover_opencode_db_path", lambda: path)
    monkeypatch.setattr(sidecar, "_discover_minimax_code_db_paths", lambda: [store])
    session_names.configure(tmp_path / "state.json")
    with caplog.at_level(logging.DEBUG):
        out: list[dict] = []
        sidecar._collect_opencode_session_names(out)
        sidecar._collect_minimax_code_session_names(out)
    assert out
    assert "secret title" not in caplog.text
    assert "Fake generated title" not in caplog.text
