"""Tests for rank_top_projects / query_projects — cross-provider project ranking."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.queries.top_projects import query_projects, rank_top_projects


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
    project,
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
            project=project,
            session_id=session_id,
            ts=ts,
            tokens_input=tokens_input,
            tokens_cache_read=tokens_cache_read,
            cost_usd=cost_usd,
            cost_cache_read=cost_cache_read,
            sidecar_id="local",
            kind=kind,
            turn_id=turn_id,
        )
    )
    session.commit()


def test_ranks_by_tokens_and_collapses_providers(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", project="alpha", tokens_input=100)
    _add(db_session, event_id="b", provider_id="gemini", project="alpha", tokens_input=400)
    _add(db_session, event_id="c", provider_id="chatgpt", project="beta", tokens_input=200)

    rows = rank_top_projects(db_session, since=_SINCE, metric="tokens").rows

    assert [r["project"] for r in rows] == ["alpha", "beta"]
    assert rows[0]["tokens_total"] == 500
    assert sorted(rows[0]["providers"]) == ["anthropic", "gemini"]


def test_excludes_null_project(db_session):
    _add(db_session, event_id="a", project="alpha", tokens_input=100)
    _add(db_session, event_id="b", project=None, tokens_input=999)

    rows = rank_top_projects(db_session, since=_SINCE, metric="tokens").rows
    assert [r["project"] for r in rows] == ["alpha"]


def test_sessions_metric_counts_distinct_sessions(db_session):
    # alpha: 3 distinct sessions; beta: 1 session with 2 messages.
    _add(db_session, event_id="a1", project="alpha", session_id="s1", tokens_input=1)
    _add(db_session, event_id="a2", project="alpha", session_id="s2", tokens_input=1)
    _add(db_session, event_id="a3", project="alpha", session_id="s3", tokens_input=1)
    _add(db_session, event_id="b1", project="beta", session_id="sb", tokens_input=9999)
    _add(db_session, event_id="b2", project="beta", session_id="sb", tokens_input=9999)

    rows = rank_top_projects(db_session, since=_SINCE, metric="sessions").rows
    assert rows[0]["project"] == "alpha"
    assert rows[0]["sessions"] == 3
    assert next(r for r in rows if r["project"] == "beta")["sessions"] == 1


def test_provider_filter_scopes_ranking(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", project="alpha", tokens_input=100)
    _add(db_session, event_id="b", provider_id="gemini", project="beta", tokens_input=400)

    rows = rank_top_projects(
        db_session, since=_SINCE, metric="tokens", provider_id="anthropic"
    ).rows
    assert [r["project"] for r in rows] == ["alpha"]


def test_cost_metric_with_exclude_cache(db_session):
    # cheap: cost is mostly cache; real: cost is fresh. exclude_cache flips order.
    _add(db_session, event_id="cheap", project="cheap", cost_usd=10.0, cost_cache_read=9.5)
    _add(db_session, event_id="real", project="real", cost_usd=5.0, cost_cache_read=0.0)

    incl = rank_top_projects(db_session, since=_SINCE, metric="cost", exclude_cache=False).rows
    assert incl[0]["project"] == "cheap"
    excl = rank_top_projects(db_session, since=_SINCE, metric="cost", exclude_cache=True).rows
    assert excl[0]["project"] == "real"


def test_query_projects_distinct_sorted(db_session):
    _add(db_session, event_id="a", project="zeta", tokens_input=1)
    _add(db_session, event_id="b", project="alpha", tokens_input=1)
    _add(db_session, event_id="c", project="alpha", tokens_input=1)
    _add(db_session, event_id="d", project=None, tokens_input=1)

    assert query_projects(db_session) == ["alpha", "zeta"]


def test_same_day_bounds_filter_top_projects_and_project_options(db_session):
    since = datetime(2026, 5, 21, 11, tzinfo=UTC)
    until = datetime(2026, 5, 21, 13, tzinfo=UTC)
    _add(
        db_session,
        event_id="before",
        project="before",
        ts=datetime(2026, 5, 21, 10, 59, tzinfo=UTC),
        tokens_input=100,
    )
    _add(
        db_session,
        event_id="inside",
        project="inside",
        ts=datetime(2026, 5, 21, 12, tzinfo=UTC),
        tokens_input=10,
    )
    _add(
        db_session,
        event_id="at-until",
        project="at-until",
        ts=until,
        tokens_input=1000,
    )

    rows = rank_top_projects(db_session, since=since, until=until, metric="tokens").rows
    assert [row["project"] for row in rows] == ["inside"]
    assert query_projects(db_session, since=since, until=until) == ["inside"]


def test_the_message_count_counts_turns(db_session):
    """A provider that emits one event per request carries its own turn id."""
    for index in range(4):
        _add(
            db_session,
            event_id=f"t{index}",
            project="alpha",
            tokens_input=100,
            turn_id="turn_1",
        )
    rows = rank_top_projects(db_session, since=_SINCE, metric="tokens").rows
    assert rows[0]["msgs"] == 1


def test_a_provider_without_a_turn_id_keeps_the_event_count(db_session):
    for index in range(3):
        _add(db_session, event_id=f"c{index}", project="alpha", tokens_input=100)
    rows = rank_top_projects(db_session, since=_SINCE, metric="tokens").rows
    assert rows[0]["msgs"] == 3


def test_a_turn_id_is_the_turns_identity(db_session):
    """A provider mints its turn ids uniquely, so the turn id alone names the
    turn — and it keeps naming it when the background-session repair moves the
    row from the task's session to its parent."""
    _add(db_session, event_id="t1", project="alpha", session_id="s1", turn_id="turn_1")
    _add(db_session, event_id="t2", project="alpha", session_id="s2", turn_id="turn_1")

    rows = rank_top_projects(db_session, since=_SINCE, metric="tokens").rows
    assert rows[0]["msgs"] == 1


def test_an_event_id_is_scoped_to_its_provider(db_session):
    """In the global mode the same event id can exist under two providers."""
    _add(db_session, event_id="shared", provider_id="anthropic", project="alpha")
    _add(db_session, event_id="shared", provider_id="gemini", project="alpha")

    rows = rank_top_projects(db_session, since=_SINCE, metric="tokens").rows
    assert rows[0]["msgs"] == 2


def test_an_empty_turn_id_falls_back_to_the_event_count(db_session):
    """A writer that stores an empty turn id must not collapse the count."""
    for index in range(3):
        _add(db_session, event_id=f"c{index}", project="alpha", turn_id="")
    rows = rank_top_projects(db_session, since=_SINCE, metric="tokens").rows
    assert rows[0]["msgs"] == 3


def test_total_counts_the_groups_the_limit_cut(db_session):
    for index in range(4):
        _add(
            db_session,
            event_id=f"p{index}",
            project=f"project-{index}",
            tokens_input=index + 1,
        )

    ranked = rank_top_projects(db_session, since=_SINCE, metric="tokens", limit=2)

    assert len(ranked.rows) == 2
    assert ranked.total == 4


def test_total_matches_the_rows_when_nothing_was_cut(db_session):
    _add(db_session, event_id="a", project="alpha", tokens_input=100)
    _add(db_session, event_id="b", project="beta", tokens_input=200)

    ranked = rank_top_projects(db_session, since=_SINCE, metric="tokens")

    assert ranked.total == len(ranked.rows) == 2


def test_total_is_zero_for_an_empty_range(db_session):
    _add(db_session, event_id="a", project="alpha", tokens_input=100)

    ranked = rank_top_projects(db_session, since=_NOW + timedelta(days=1), metric="tokens")

    assert ranked.rows == []
    assert ranked.total == 0


def test_total_follows_the_provider_filter(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", project="alpha", tokens_input=100)
    _add(db_session, event_id="b", provider_id="gemini", project="beta", tokens_input=200)

    scoped = rank_top_projects(db_session, since=_SINCE, metric="tokens", provider_id="gemini")

    assert [r["project"] for r in scoped.rows] == ["beta"]
    assert scoped.total == 1
