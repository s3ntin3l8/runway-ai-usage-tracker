"""Tests for rank_top_hosts — cross-provider host ranking."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.queries.top_hosts import rank_top_hosts


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
    sidecar_id="local",
    session_id="s1",
    ts=_NOW,
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
            sidecar_id=sidecar_id,
            session_id=session_id,
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


def test_ranks_hosts_by_tokens(db_session):
    _add(db_session, event_id="a", sidecar_id="host-a", tokens_input=100)
    _add(db_session, event_id="b", sidecar_id="host-b", tokens_input=400)
    _add(db_session, event_id="c", sidecar_id="host-b", tokens_input=200)

    rows = rank_top_hosts(db_session, since=_SINCE, metric="tokens").rows

    assert [r["sidecar_id"] for r in rows] == ["host-b", "host-a"]
    assert rows[0]["tokens_total"] == 600


def test_excludes_an_empty_sidecar_id(db_session):
    _add(db_session, event_id="a", sidecar_id="host-a", tokens_input=100)
    _add(db_session, event_id="b", sidecar_id="", tokens_input=999)

    rows = rank_top_hosts(db_session, since=_SINCE, metric="tokens").rows
    assert [r["sidecar_id"] for r in rows] == ["host-a"]


def test_counts_the_distinct_sessions_on_a_host(db_session):
    _add(db_session, event_id="a", sidecar_id="host-a", session_id="s1")
    _add(db_session, event_id="b", sidecar_id="host-a", session_id="s2")
    _add(db_session, event_id="c", sidecar_id="host-a", session_id="s1")

    row = rank_top_hosts(db_session, since=_SINCE, metric="tokens").rows[0]
    assert row["sessions"] == 2


def test_collapses_providers_and_reports_them(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", sidecar_id="host-a")
    _add(db_session, event_id="b", provider_id="gemini", sidecar_id="host-a")

    row = rank_top_hosts(db_session, since=_SINCE, metric="tokens").rows[0]
    assert sorted(row["providers"]) == ["anthropic", "gemini"]


def test_provider_filter_scopes_ranking(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", sidecar_id="host-a", tokens_input=100)
    _add(db_session, event_id="b", provider_id="gemini", sidecar_id="host-b", tokens_input=400)

    rows = rank_top_hosts(db_session, since=_SINCE, metric="tokens", provider_id="anthropic").rows
    assert [r["sidecar_id"] for r in rows] == ["host-a"]


def test_sidecar_filter_scopes_ranking(db_session):
    _add(db_session, event_id="a", sidecar_id="host-a", tokens_input=100)
    _add(db_session, event_id="b", sidecar_id="host-b", tokens_input=400)

    rows = rank_top_hosts(db_session, since=_SINCE, metric="tokens", sidecar_id="host-b").rows
    assert [r["sidecar_id"] for r in rows] == ["host-b"]


def test_cost_metric_with_exclude_cache(db_session):
    _add(db_session, event_id="cheap", sidecar_id="host-cheap", cost_usd=10.0, cost_cache_read=9.5)
    _add(db_session, event_id="real", sidecar_id="host-real", cost_usd=5.0, cost_cache_read=0.0)

    incl = rank_top_hosts(db_session, since=_SINCE, metric="cost", exclude_cache=False).rows
    assert incl[0]["sidecar_id"] == "host-cheap"
    excl = rank_top_hosts(db_session, since=_SINCE, metric="cost", exclude_cache=True).rows
    assert excl[0]["sidecar_id"] == "host-real"


def test_same_day_bounds_filter_the_ranking(db_session):
    since = datetime(2026, 5, 21, 11, tzinfo=UTC)
    until = datetime(2026, 5, 21, 13, tzinfo=UTC)
    _add(
        db_session,
        event_id="before",
        sidecar_id="host-before",
        ts=datetime(2026, 5, 21, 10, 59, tzinfo=UTC),
        tokens_input=100,
    )
    _add(
        db_session,
        event_id="inside",
        sidecar_id="host-inside",
        ts=datetime(2026, 5, 21, 12, tzinfo=UTC),
        tokens_input=10,
    )
    _add(db_session, event_id="at-until", sidecar_id="host-at-until", ts=until, tokens_input=1000)

    rows = rank_top_hosts(db_session, since=since, until=until, metric="tokens").rows
    assert [r["sidecar_id"] for r in rows] == ["host-inside"]


def test_reports_the_first_and_last_event_of_the_host(db_session):
    _add(
        db_session,
        event_id="a",
        sidecar_id="host-a",
        ts=datetime(2026, 5, 21, 10, 0, tzinfo=UTC),
        tokens_input=1,
    )
    _add(
        db_session,
        event_id="b",
        sidecar_id="host-a",
        ts=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
        tokens_input=1,
    )

    row = rank_top_hosts(db_session, since=_SINCE, metric="tokens").rows[0]
    assert row["ts_start"] == "2026-05-21T10:00:00+00:00"
    assert row["ts_end"] == "2026-05-21T12:00:00+00:00"


def test_limit_caps_the_ranking(db_session):
    for index in range(5):
        _add(db_session, event_id=f"e{index}", sidecar_id=f"host-{index}", tokens_input=index + 1)

    rows = rank_top_hosts(db_session, since=_SINCE, metric="tokens", limit=2).rows
    assert len(rows) == 2
    assert rows[0]["sidecar_id"] == "host-4"


def test_the_message_count_counts_turns(db_session):
    """A provider that emits one event per request carries its own turn id."""
    for index in range(4):
        _add(
            db_session,
            event_id=f"t{index}",
            sidecar_id="host-a",
            tokens_input=100,
            turn_id="turn_1",
        )
    rows = rank_top_hosts(db_session, since=_SINCE, metric="tokens").rows
    assert rows[0]["msgs"] == 1


def test_total_counts_the_groups_the_limit_cut(db_session):
    for index in range(5):
        _add(db_session, event_id=f"e{index}", sidecar_id=f"host-{index}", tokens_input=index + 1)

    ranked = rank_top_hosts(db_session, since=_SINCE, metric="tokens", limit=2)

    assert len(ranked.rows) == 2
    assert ranked.total == 5


def test_total_matches_the_rows_when_nothing_was_cut(db_session):
    _add(db_session, event_id="a", sidecar_id="host-a", tokens_input=100)
    _add(db_session, event_id="b", sidecar_id="host-b", tokens_input=200)

    ranked = rank_top_hosts(db_session, since=_SINCE, metric="tokens")

    assert ranked.total == len(ranked.rows) == 2


def test_total_is_zero_for_an_empty_range(db_session):
    _add(db_session, event_id="a", sidecar_id="host-a", tokens_input=100)

    ranked = rank_top_hosts(db_session, since=_NOW + timedelta(days=1), metric="tokens")

    assert ranked.rows == []
    assert ranked.total == 0


def test_total_follows_the_provider_filter(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", sidecar_id="host-a", tokens_input=100)
    _add(db_session, event_id="b", provider_id="gemini", sidecar_id="host-b", tokens_input=200)

    scoped = rank_top_hosts(db_session, since=_SINCE, metric="tokens", provider_id="gemini")

    assert [r["sidecar_id"] for r in scoped.rows] == ["host-b"]
    assert scoped.total == 1
