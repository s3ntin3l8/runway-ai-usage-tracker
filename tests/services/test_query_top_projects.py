"""Tests for query_top_projects / query_projects — cross-provider project ranking."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.queries.top_projects import query_projects, query_top_projects


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
        )
    )
    session.commit()


def test_ranks_by_tokens_and_collapses_providers(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", project="alpha", tokens_input=100)
    _add(db_session, event_id="b", provider_id="gemini", project="alpha", tokens_input=400)
    _add(db_session, event_id="c", provider_id="chatgpt", project="beta", tokens_input=200)

    rows = query_top_projects(db_session, since=_SINCE, metric="tokens")

    assert [r["project"] for r in rows] == ["alpha", "beta"]
    assert rows[0]["tokens_total"] == 500
    assert sorted(rows[0]["providers"]) == ["anthropic", "gemini"]


def test_excludes_null_project(db_session):
    _add(db_session, event_id="a", project="alpha", tokens_input=100)
    _add(db_session, event_id="b", project=None, tokens_input=999)

    rows = query_top_projects(db_session, since=_SINCE, metric="tokens")
    assert [r["project"] for r in rows] == ["alpha"]


def test_sessions_metric_counts_distinct_sessions(db_session):
    # alpha: 3 distinct sessions; beta: 1 session with 2 messages.
    _add(db_session, event_id="a1", project="alpha", session_id="s1", tokens_input=1)
    _add(db_session, event_id="a2", project="alpha", session_id="s2", tokens_input=1)
    _add(db_session, event_id="a3", project="alpha", session_id="s3", tokens_input=1)
    _add(db_session, event_id="b1", project="beta", session_id="sb", tokens_input=9999)
    _add(db_session, event_id="b2", project="beta", session_id="sb", tokens_input=9999)

    rows = query_top_projects(db_session, since=_SINCE, metric="sessions")
    assert rows[0]["project"] == "alpha"
    assert rows[0]["sessions"] == 3
    assert next(r for r in rows if r["project"] == "beta")["sessions"] == 1


def test_provider_filter_scopes_ranking(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", project="alpha", tokens_input=100)
    _add(db_session, event_id="b", provider_id="gemini", project="beta", tokens_input=400)

    rows = query_top_projects(db_session, since=_SINCE, metric="tokens", provider_id="anthropic")
    assert [r["project"] for r in rows] == ["alpha"]


def test_cost_metric_with_exclude_cache(db_session):
    # cheap: cost is mostly cache; real: cost is fresh. exclude_cache flips order.
    _add(db_session, event_id="cheap", project="cheap", cost_usd=10.0, cost_cache_read=9.5)
    _add(db_session, event_id="real", project="real", cost_usd=5.0, cost_cache_read=0.0)

    incl = query_top_projects(db_session, since=_SINCE, metric="cost", exclude_cache=False)
    assert incl[0]["project"] == "cheap"
    excl = query_top_projects(db_session, since=_SINCE, metric="cost", exclude_cache=True)
    assert excl[0]["project"] == "real"


def test_query_projects_distinct_sorted(db_session):
    _add(db_session, event_id="a", project="zeta", tokens_input=1)
    _add(db_session, event_id="b", project="alpha", tokens_input=1)
    _add(db_session, event_id="c", project="alpha", tokens_input=1)
    _add(db_session, event_id="d", project=None, tokens_input=1)

    assert query_projects(db_session) == ["alpha", "zeta"]
