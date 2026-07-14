"""Unit tests for the OpenCode event extractor."""

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.sidecar_pkg.event_extractors.opencode import (
    _classify_opencode_error,
    map_opencode_provider_id,
    parse_opencode_events,
)
from tests.fixtures.opencode_fixture import make_opencode_db


def _make_db() -> tuple[Path, sqlite3.Connection]:
    """Create a temp SQLite file with the OpenCode schema and sample data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = make_opencode_db(str(db_path))
    conn.close()
    return db_path, conn


def _build_db(messages: list[dict]) -> Path:
    """Build a minimal OpenCode-shaped SQLite DB from raw message dicts."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    for msg in messages:
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?,?,?,?,?)",
            (
                msg["id"],
                msg.get("session_id"),
                msg["time_created"],
                msg["time_created"],
                json.dumps(msg["data"]),
            ),
        )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------


def test_extracts_messages():
    """Assistant messages are extracted; user messages are ignored."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        assert len(evts) == 2  # 2 assistant messages, 1 user message (ignored)
        assert all(e.provider_id == "opencode" for e in evts)
    finally:
        db_path.unlink(missing_ok=True)


def test_uses_log_cost_when_present():
    """cost_usd on the event is taken from the logged value, not computed."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        # Fixture msg_opencode_001 has cost=0.0042
        msg1 = next(e for e in evts if e.event_id == "msg_opencode_001")
        assert msg1.cost_usd == pytest.approx(0.0042, rel=1e-4)

        # Fixture msg_opencode_002 has cost=0.0088
        msg2 = next(e for e in evts if e.event_id == "msg_opencode_002")
        assert msg2.cost_usd == pytest.approx(0.0088, rel=1e-4)
    finally:
        db_path.unlink(missing_ok=True)


def test_session_id_from_db():
    """session_id comes from the message.session_id column in the DB."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session_ids = {e.session_id for e in evts}
        assert "ses_session_abc" in session_ids
    finally:
        db_path.unlink(missing_ok=True)


def test_filters_by_since():
    """Events at or before since are excluded."""
    db_path, _ = _make_db()
    try:
        # msg_opencode_001 ts=2026-05-08T14:01:00Z (epoch_ms=1778248860000)
        # msg_opencode_002 ts=2026-05-08T14:03:00Z (epoch_ms=1778248980000)
        cutoff = datetime(2026, 5, 8, 14, 2, 0, tzinfo=UTC)
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=cutoff,
        )
        # Only msg_opencode_002 is after the cutoff
        assert len(evts) == 1
        assert evts[0].event_id == "msg_opencode_002"
    finally:
        db_path.unlink(missing_ok=True)


def test_captures_token_dimensions():
    """Token fields are correctly populated from the nested tokens dict."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        msg1 = next(e for e in evts if e.event_id == "msg_opencode_001")
        assert msg1.tokens_input == 1200
        assert msg1.tokens_output == 300
        assert msg1.tokens_reasoning == 0
        assert msg1.tokens_cache_read == 500
        assert msg1.tokens_cache_create == 0

        msg2 = next(e for e in evts if e.event_id == "msg_opencode_002")
        assert msg2.tokens_input == 2500
        assert msg2.tokens_output == 700
        assert msg2.tokens_reasoning == 150
        assert msg2.tokens_cache_read == 1200
    finally:
        db_path.unlink(missing_ok=True)


def test_captures_cwd_and_latency():
    """cwd comes from data.path; latency_ms = time.completed − time.created (ms)."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        m1 = next(e for e in evts if e.event_id == "msg_opencode_001")
        assert m1.cwd == "/home/user/project"
        assert m1.latency_ms == 2000  # 1746709262000 − 1746709260000
        m2 = next(e for e in evts if e.event_id == "msg_opencode_002")
        assert m2.latency_ms == 3500  # 1746709383500 − 1746709380000
    finally:
        db_path.unlink(missing_ok=True)


def test_nonexistent_db_returns_empty():
    """Missing DB file returns empty list without raising."""
    evts = parse_opencode_events(
        Path("/does/not/exist.db"),
        account_id="default",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert evts == []


# ---------------------------------------------------------------------------
# providerID -> runway provider_id mapping (issue #182)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "oc_provider_id,expected",
    [
        ("opencode", "opencode-free"),
        ("opencode-go", "opencode"),
        ("open-design-byok", "opencode-byok"),
        ("openrouter", "opencode-openrouter"),
        ("ollama-cloud", "opencode-ollama"),
        ("OPENCODE-GO", "opencode"),  # case-insensitive
        ("some-future-backend", "opencode-some-future-backend"),  # unknown -> derived, not Go
        ("", "opencode"),  # missing/empty -> historical default
    ],
)
def test_map_opencode_provider_id(oc_provider_id, expected):
    assert map_opencode_provider_id(oc_provider_id) == expected


def test_unrecognized_provider_never_collapses_into_go():
    """An unrecognized providerID must never resolve to the Go tier's 'opencode'."""
    assert map_opencode_provider_id("some-new-backend") != "opencode"


def _byok_message(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "session_id": "ses_byok",
        "time_created": 1778248860000,
        "data": {
            "role": "assistant",
            "path": {"cwd": "/home/user/project"},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "modelID": "tencent/hy3:free",
            "providerID": "open-design-byok",
            "time": {"created": 1746709260000, "completed": 1746709262000},
        },
    }


def test_byok_provider_gets_its_own_id():
    db_path = _build_db([_byok_message("msg_byok_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "opencode-byok"
        assert evts[0].kind == "message"
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Failed-request handling (issue #182): errors don't count as usage
# ---------------------------------------------------------------------------


def _error_message(msg_id: str, provider_id: str, status_code: int) -> dict:
    return {
        "id": msg_id,
        "session_id": "ses_err",
        "time_created": 1778248860000,
        "data": {
            "role": "assistant",
            "path": {"cwd": "/home/user/project"},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "modelID": "glm-5.1",
            "providerID": provider_id,
            "time": {"created": 1746709260000},
            "error": {
                "name": "APIError",
                "data": {"message": "boom", "statusCode": status_code},
            },
        },
    }


def test_classify_opencode_error():
    assert _classify_opencode_error({"data": {"statusCode": 401}}) == "auth_failed"
    assert _classify_opencode_error({"data": {"statusCode": 403}}) == "quota_exceeded"
    assert _classify_opencode_error({"data": {"statusCode": 429}}) == "rate_limit"
    assert _classify_opencode_error({"data": {"statusCode": 504}}) == "timeout"
    assert _classify_opencode_error({"data": {"statusCode": 500}}) == "http_500"
    assert _classify_opencode_error({"name": "NetworkError"}) == "networkerror"
    assert _classify_opencode_error({}) == "unknown_error"


def test_failed_request_pushed_as_error_kind_not_usage():
    """A failed openrouter/ollama request must not count as a message with usage."""
    db_path = _build_db(
        [
            _error_message("msg_openrouter_401", "openrouter", 401),
            _error_message("msg_ollama_403", "ollama-cloud", 403),
        ]
    )
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 2
        by_id = {e.event_id: e for e in evts}

        or_evt = by_id["msg_openrouter_401"]
        assert or_evt.provider_id == "opencode-openrouter"
        assert or_evt.kind == "error"
        assert or_evt.error_reason == "auth_failed"
        assert or_evt.tokens_input == 0
        assert or_evt.cost_usd is None

        ol_evt = by_id["msg_ollama_403"]
        assert ol_evt.provider_id == "opencode-ollama"
        assert ol_evt.kind == "error"
        assert ol_evt.error_reason == "quota_exceeded"
    finally:
        db_path.unlink(missing_ok=True)


def test_successful_request_not_marked_as_error():
    """Sanity check: a message without an `error` field stays kind='message'."""
    db_path = _build_db([_byok_message("msg_ok_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert evts[0].kind == "message"
        assert evts[0].error_reason is None
    finally:
        db_path.unlink(missing_ok=True)
