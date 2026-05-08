"""Unit tests for the OpenCode event extractor."""

import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from scripts.sidecar_pkg.event_extractors.opencode import parse_opencode_events
from tests.fixtures.opencode_fixture import make_opencode_db


def _make_db() -> tuple[Path, sqlite3.Connection]:
    """Create a temp SQLite file with the OpenCode schema and sample data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = make_opencode_db(str(db_path))
    conn.close()
    return db_path, conn


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


def test_nonexistent_db_returns_empty():
    """Missing DB file returns empty list without raising."""
    evts = parse_opencode_events(
        Path("/does/not/exist.db"),
        account_id="default",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert evts == []


# ---------------------------------------------------------------------------
# Import pytest for approx
# ---------------------------------------------------------------------------

import pytest  # noqa: E402
