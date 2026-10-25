"""Tests for rank_top_tools — tool-name ranking via json_each over tools_json."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.queries.top_tools import rank_top_tools


@pytest.fixture
def db_session():
    fd, db_path = tempfile.mkstemp()
    os.close(fd)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()
        os.remove(db_path)


_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
_SINCE = _NOW - timedelta(days=1)


def _add(session, *, event_id, tools_json, ts=_NOW, kind="message", provider_id="anthropic"):
    session.add(
        UsageEvent(
            provider_id=provider_id,
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

    rows = rank_top_tools(db_session, since=_SINCE).rows
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

    rows = rank_top_tools(db_session, since=_SINCE).rows
    assert len(rows) == 1
    assert rows[0]["tool"] == "Write"
    assert rows[0]["calls"] == 1


def test_limit_caps_rows(db_session):
    _add(db_session, event_id="e", tools_json='["A", "A", "A", "B", "B", "C"]')
    rows = rank_top_tools(db_session, since=_SINCE, limit=2).rows
    assert [r["tool"] for r in rows] == ["A", "B"]


def test_same_day_bounds_include_current_day_and_exclude_until(db_session):
    since = datetime(2026, 5, 21, 11, tzinfo=UTC)
    until = datetime(2026, 5, 21, 13, tzinfo=UTC)
    _add(
        db_session,
        event_id="inside",
        tools_json='["Read"]',
        ts=datetime(2026, 5, 21, 12, tzinfo=UTC),
    )
    _add(db_session, event_id="at-until", tools_json='["Write"]', ts=until)

    rows = rank_top_tools(db_session, since=since, until=until).rows
    assert rows == [{"tool": "Read", "calls": 1, "msgs": 1}]


def test_total_counts_the_groups_the_limit_cut(db_session):
    _add(db_session, event_id="e", tools_json='["A", "B", "C", "D"]')

    ranked = rank_top_tools(db_session, since=_SINCE, limit=2)

    assert len(ranked.rows) == 2
    assert ranked.total == 4


def test_total_matches_the_rows_when_nothing_was_cut(db_session):
    _add(db_session, event_id="e", tools_json='["Read", "Write"]')

    ranked = rank_top_tools(db_session, since=_SINCE)

    assert ranked.total == len(ranked.rows) == 2


def test_total_is_zero_for_an_empty_range(db_session):
    _add(db_session, event_id="e", tools_json='["Read"]')

    ranked = rank_top_tools(db_session, since=_NOW + timedelta(days=1))

    assert ranked.rows == []
    assert ranked.total == 0


def test_total_ignores_an_unnamed_tool(db_session):
    """An unnamed tool is not a tool — and it must not inflate the total."""
    _add(db_session, event_id="e", tools_json='["Read", ""]')

    ranked = rank_top_tools(db_session, since=_SINCE)

    assert [r["tool"] for r in ranked.rows] == ["Read"]
    assert ranked.total == 1


def test_total_ignores_a_null_tool_name(db_session):
    _add(db_session, event_id="e", tools_json='["Read", null]')

    ranked = rank_top_tools(db_session, since=_SINCE)

    assert [r["tool"] for r in ranked.rows] == ["Read"]
    assert ranked.total == 1


def test_total_ignores_a_tool_name_that_is_not_a_string(db_session):
    """A JSON false is not a tool name — and it must not inflate the total.

    ``json_each`` hands a JSON false back as the integer 0, which is not
    equal to '' and so passes a plain inequality. Without the type check it
    would be counted by the window function and then dropped from the rows,
    and the total would exceed the rows.
    """
    _add(db_session, event_id="e", tools_json='["Read", false]')

    ranked = rank_top_tools(db_session, since=_SINCE)

    assert [r["tool"] for r in ranked.rows] == ["Read"]
    assert ranked.total == 1


def test_total_follows_the_provider_filter(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", tools_json='["Read"]')
    _add(db_session, event_id="b", provider_id="gemini", tools_json='["Write"]')

    scoped = rank_top_tools(db_session, since=_SINCE, provider_id="gemini")

    assert [r["tool"] for r in scoped.rows] == ["Write"]
    assert scoped.total == 1
