"""Tests for query_top_tools — tool-name ranking via json_each over tools_json."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.queries.top_tools import query_top_tools


@pytest.fixture
def db_session():
    fd, db_path = tempfile.mkstemp()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
_SINCE = _NOW - timedelta(days=1)


def _add(session, *, event_id, tools_json, ts=_NOW, kind="message"):
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc1",
            event_id=event_id,
            tools_json=tools_json,
            ts=ts,
            sidecar_id="local",
            kind=kind,
        )
    )
    session.commit()


def test_counts_tool_invocations_across_events(db_session):
    _add(db_session, event_id="e1", tools_json='["Bash", "Read"]')
    _add(db_session, event_id="e2", tools_json='["Bash", "Bash"]')  # 2 Bash calls in one msg
    _add(db_session, event_id="e3", tools_json='["Read"]')

    rows = query_top_tools(db_session, since=_SINCE)
    by_tool = {r["tool"]: r for r in rows}

    assert by_tool["Bash"]["calls"] == 3
    assert by_tool["Bash"]["msgs"] == 2  # appeared in 2 distinct messages
    assert by_tool["Read"]["calls"] == 2
    # Ordered by calls desc.
    assert rows[0]["tool"] == "Bash"


def test_ignores_null_and_out_of_range_and_errors(db_session):
    _add(db_session, event_id="ok", tools_json='["Write"]')
    _add(db_session, event_id="nulltools", tools_json=None)
    _add(db_session, event_id="old", tools_json='["Write"]', ts=_NOW - timedelta(days=10))
    _add(db_session, event_id="err", tools_json='["Write"]', kind="error")

    rows = query_top_tools(db_session, since=_SINCE)
    assert len(rows) == 1
    assert rows[0]["tool"] == "Write"
    assert rows[0]["calls"] == 1


def test_limit_caps_rows(db_session):
    _add(db_session, event_id="e", tools_json='["A", "A", "A", "B", "B", "C"]')
    rows = query_top_tools(db_session, since=_SINCE, limit=2)
    assert [r["tool"] for r in rows] == ["A", "B"]
