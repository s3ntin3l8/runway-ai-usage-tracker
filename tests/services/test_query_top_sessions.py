"""Tests for query_top_sessions — cross-provider session ranking."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.queries.top_sessions import query_top_sessions


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


def _add(
    session,
    *,
    event_id,
    provider_id="anthropic",
    session_id="s1",
    project="alpha",
    cwd="/repo/alpha",
    sidecar_id="local",
    ts=_NOW,
    model_id=None,
    tokens_input=0,
    tokens_cache_read=0,
    cost_usd=0.0,
    cost_cache_read=0.0,
    kind="message",
    turn_id=None,
):
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id="acc1",
            event_id=event_id,
            session_id=session_id,
            project=project,
            cwd=cwd,
            sidecar_id=sidecar_id,
            model_id=model_id,
            ts=ts,
            tokens_input=tokens_input,
            tokens_cache_read=tokens_cache_read,
            cost_usd=cost_usd,
            cost_cache_read=cost_cache_read,
            kind=kind,
            turn_id=turn_id,
        )
    )
    session.commit()


def test_ranks_by_tokens_and_collapses_providers(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", session_id="s1", tokens_input=100)
    _add(db_session, event_id="b", provider_id="gemini", session_id="s1", tokens_input=400)
    _add(db_session, event_id="c", provider_id="chatgpt", session_id="s2", tokens_input=200)

    rows = query_top_sessions(db_session, since=_SINCE, metric="tokens")

    assert [r["session_id"] for r in rows] == ["s1", "s2"]
    assert rows[0]["tokens_total"] == 500
    assert sorted(rows[0]["providers"]) == ["anthropic", "gemini"]


def test_excludes_null_session_id(db_session):
    _add(db_session, event_id="a", session_id="s1", tokens_input=100)
    _add(db_session, event_id="b", session_id=None, tokens_input=999)

    rows = query_top_sessions(db_session, since=_SINCE, metric="tokens")
    assert [r["session_id"] for r in rows] == ["s1"]


def test_excludes_an_empty_session_id(db_session):
    """A blank id is not a session either — it would collapse every such event
    into one phantom session with an empty label."""
    _add(db_session, event_id="a", session_id="s1", tokens_input=100)
    _add(db_session, event_id="b", session_id="", tokens_input=999)

    rows = query_top_sessions(db_session, since=_SINCE, metric="tokens")
    assert [r["session_id"] for r in rows] == ["s1"]


def test_describes_the_session_by_its_latest_event(db_session):
    """A session that moved between hosts is described by where it ended."""
    _add(
        db_session,
        event_id="early",
        session_id="s1",
        project="zulu",
        cwd="/repo/zulu",
        sidecar_id="zzz-host",
        ts=datetime(2026, 5, 21, 10, 0, tzinfo=UTC),
        tokens_input=10,
    )
    _add(
        db_session,
        event_id="late",
        session_id="s1",
        project="alpha",
        cwd="/repo/alpha",
        sidecar_id="aaa-host",
        ts=datetime(2026, 5, 21, 11, 0, tzinfo=UTC),
        tokens_input=10,
    )

    row = query_top_sessions(db_session, since=_SINCE, metric="tokens")[0]
    assert row["sidecar_id"] == "aaa-host"
    assert row["project"] == "alpha"
    assert row["cwd"] == "/repo/alpha"


def test_falls_back_when_the_latest_event_has_no_project(db_session):
    """The latest event may carry no cwd, and no project derives from it. The
    session still knows its project from an earlier event."""
    _add(
        db_session,
        event_id="early",
        session_id="s1",
        project="alpha",
        cwd="/repo/alpha",
        sidecar_id="host-a",
        ts=datetime(2026, 5, 21, 10, 0, tzinfo=UTC),
        tokens_input=10,
    )
    _add(
        db_session,
        event_id="late",
        session_id="s1",
        project=None,
        cwd=None,
        sidecar_id="host-b",
        ts=datetime(2026, 5, 21, 11, 0, tzinfo=UTC),
        tokens_input=10,
    )

    row = query_top_sessions(db_session, since=_SINCE, metric="tokens")[0]
    # The host still comes from the latest event...
    assert row["sidecar_id"] == "host-b"
    # ...but the project falls back to the best one the session had.
    assert row["project"] == "alpha"
    assert row["cwd"] == "/repo/alpha"


def test_counts_the_hosts_a_session_ran_on(db_session):
    _add(
        db_session,
        event_id="early",
        session_id="s1",
        sidecar_id="host-a",
        ts=datetime(2026, 5, 21, 10, 0, tzinfo=UTC),
        tokens_input=10,
    )
    _add(
        db_session,
        event_id="late",
        session_id="s1",
        sidecar_id="host-b",
        ts=datetime(2026, 5, 21, 11, 0, tzinfo=UTC),
        tokens_input=10,
    )
    _add(
        db_session,
        event_id="other",
        session_id="s2",
        sidecar_id="host-a",
        ts=datetime(2026, 5, 21, 11, 0, tzinfo=UTC),
        tokens_input=10,
    )

    rows = query_top_sessions(db_session, since=_SINCE, metric="tokens")
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["hosts"] == 2
    assert rows[1]["hosts"] == 1


def test_carries_the_project_host_and_models(db_session):
    _add(
        db_session,
        event_id="a",
        session_id="s1",
        project="alpha",
        cwd="/repo/alpha",
        sidecar_id="workstation",
        model_id="opus-4.8",
        tokens_input=10,
    )

    row = query_top_sessions(db_session, since=_SINCE, metric="tokens")[0]
    assert row["project"] == "alpha"
    assert row["cwd"] == "/repo/alpha"
    assert row["sidecar_id"] == "workstation"
    assert row["models"] == ["opus-4.8"]


def test_reports_the_first_and_last_event_of_the_session(db_session):
    _add(
        db_session,
        event_id="a",
        session_id="s1",
        ts=datetime(2026, 5, 21, 10, 0, tzinfo=UTC),
        tokens_input=1,
    )
    _add(
        db_session,
        event_id="b",
        session_id="s1",
        ts=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
        tokens_input=1,
    )

    row = query_top_sessions(db_session, since=_SINCE, metric="tokens")[0]
    assert row["ts_start"] == "2026-05-21T10:00:00+00:00"
    assert row["ts_end"] == "2026-05-21T12:00:00+00:00"


def test_provider_filter_scopes_ranking(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", session_id="s1", tokens_input=100)
    _add(db_session, event_id="b", provider_id="gemini", session_id="s2", tokens_input=400)

    rows = query_top_sessions(db_session, since=_SINCE, metric="tokens", provider_id="anthropic")
    assert [r["session_id"] for r in rows] == ["s1"]


def test_sidecar_filter_scopes_ranking(db_session):
    _add(db_session, event_id="a", session_id="s1", sidecar_id="host-a", tokens_input=100)
    _add(db_session, event_id="b", session_id="s2", sidecar_id="host-b", tokens_input=400)

    rows = query_top_sessions(db_session, since=_SINCE, metric="tokens", sidecar_id="host-b")
    assert [r["session_id"] for r in rows] == ["s2"]


def test_cost_metric_with_exclude_cache(db_session):
    _add(
        db_session,
        event_id="cheap",
        session_id="s-cheap",
        cost_usd=10.0,
        cost_cache_read=9.5,
    )
    _add(db_session, event_id="real", session_id="s-real", cost_usd=5.0, cost_cache_read=0.0)

    incl = query_top_sessions(db_session, since=_SINCE, metric="cost", exclude_cache=False)
    assert incl[0]["session_id"] == "s-cheap"
    excl = query_top_sessions(db_session, since=_SINCE, metric="cost", exclude_cache=True)
    assert excl[0]["session_id"] == "s-real"


def test_same_day_bounds_filter_the_ranking(db_session):
    since = datetime(2026, 5, 21, 11, tzinfo=UTC)
    until = datetime(2026, 5, 21, 13, tzinfo=UTC)
    _add(
        db_session,
        event_id="before",
        session_id="s-before",
        ts=datetime(2026, 5, 21, 10, 59, tzinfo=UTC),
        tokens_input=100,
    )
    _add(
        db_session,
        event_id="inside",
        session_id="s-inside",
        ts=datetime(2026, 5, 21, 12, tzinfo=UTC),
        tokens_input=10,
    )
    _add(
        db_session,
        event_id="at-until",
        session_id="s-at-until",
        ts=until,
        tokens_input=1000,
    )

    rows = query_top_sessions(db_session, since=since, until=until, metric="tokens")
    assert [r["session_id"] for r in rows] == ["s-inside"]


def test_limit_caps_the_ranking(db_session):
    for index in range(5):
        _add(db_session, event_id=f"e{index}", session_id=f"s{index}", tokens_input=index + 1)

    rows = query_top_sessions(db_session, since=_SINCE, metric="tokens", limit=2)
    assert len(rows) == 2
    assert rows[0]["session_id"] == "s4"


def test_the_message_count_counts_turns(db_session):
    """A provider that emits one event per request carries its own turn id."""
    for index in range(4):
        _add(
            db_session,
            event_id=f"t{index}",
            session_id="s1",
            tokens_input=100,
            turn_id="turn_1",
        )
    rows = query_top_sessions(db_session, since=_SINCE, metric="tokens")
    assert rows[0]["msgs"] == 1


def test_an_event_id_is_scoped_to_its_provider(db_session):
    """In the global mode the same event id can exist under two providers."""
    _add(db_session, event_id="shared", provider_id="anthropic", session_id="s1")
    _add(db_session, event_id="shared", provider_id="gemini", session_id="s1")

    rows = query_top_sessions(db_session, since=_SINCE, metric="tokens")
    assert rows[0]["msgs"] == 2
