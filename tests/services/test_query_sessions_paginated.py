"""Tests for query_sessions_paginated + the project/cwd fields on query_sessions."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.queries.sessions import query_sessions, query_sessions_paginated


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
_SINCE = _NOW - timedelta(days=2)


def _add(session, *, session_id, project=None, cwd=None, git_branch=None, ts=_NOW):
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc1",
            event_id=f"{session_id}-{ts.isoformat()}",
            session_id=session_id,
            project=project,
            cwd=cwd,
            git_branch=git_branch,
            ts=ts,
            tokens_input=100,
            model_id="opus",
            sidecar_id="local",
            kind="message",
        )
    )
    session.commit()


def _kwargs():
    return {"provider_id": "anthropic", "account_id": "acc1", "since": _SINCE}


def test_query_sessions_surfaces_project_context(db_session):
    _add(db_session, session_id="s1", project="runway", cwd="/home/u/runway", git_branch="main")
    rows = query_sessions(db_session, **_kwargs())
    assert rows[0]["project"] == "runway"
    assert rows[0]["cwd"] == "/home/u/runway"
    assert rows[0]["git_branch"] == "main"


def test_pagination_total_and_offset(db_session):
    # 5 sessions, each at a distinct minute so ordering is stable.
    for i in range(5):
        _add(db_session, session_id=f"s{i}", ts=_NOW - timedelta(minutes=i))

    p0 = query_sessions_paginated(db_session, **_kwargs(), page=0, page_size=2, sort_by="recent")
    assert p0["total"] == 5
    assert len(p0["sessions"]) == 2
    assert [s["session_id"] for s in p0["sessions"]] == ["s0", "s1"]

    p1 = query_sessions_paginated(db_session, **_kwargs(), page=1, page_size=2, sort_by="recent")
    assert [s["session_id"] for s in p1["sessions"]] == ["s2", "s3"]

    p2 = query_sessions_paginated(db_session, **_kwargs(), page=2, page_size=2, sort_by="recent")
    assert [s["session_id"] for s in p2["sessions"]] == ["s4"]


def _add_event(session, *, session_id, ts, tokens=100, cost=0.0):
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc1",
            event_id=f"{session_id}-{ts.isoformat()}",
            session_id=session_id,
            ts=ts,
            tokens_input=tokens,
            cost_usd=cost,
            model_id="opus",
            sidecar_id="local",
            kind="message",
        )
    )
    session.commit()


def _seed_sortable(session):
    """Three sessions with deliberately divergent duration/msgs/tokens/cost so
    each sort key yields a distinct ordering."""
    # short: 1 msg, 0s duration, 100 tok, $0.10
    _add_event(session, session_id="short", ts=_NOW, tokens=100, cost=0.10)
    # mid: 2 msgs, 600s duration, 500 tok, $0.50
    _add_event(session, session_id="mid", ts=_NOW - timedelta(minutes=10), tokens=200, cost=0.20)
    _add_event(session, session_id="mid", ts=_NOW, tokens=300, cost=0.30)
    # long: 3 msgs, 1800s duration, 200 tok, $1.00
    _add_event(session, session_id="long", ts=_NOW - timedelta(minutes=30), tokens=50, cost=0.40)
    _add_event(session, session_id="long", ts=_NOW - timedelta(minutes=15), tokens=50, cost=0.30)
    _add_event(session, session_id="long", ts=_NOW, tokens=100, cost=0.30)


def _order(session, *, sort_by, sort_dir="desc"):
    res = query_sessions_paginated(session, **_kwargs(), sort_by=sort_by, sort_dir=sort_dir)
    return [s["session_id"] for s in res["sessions"]]


def test_sort_by_columns_desc(db_session):
    _seed_sortable(db_session)
    assert _order(db_session, sort_by="duration") == ["long", "mid", "short"]
    assert _order(db_session, sort_by="messages") == ["long", "mid", "short"]
    assert _order(db_session, sort_by="tokens") == ["mid", "long", "short"]
    assert _order(db_session, sort_by="cost") == ["long", "mid", "short"]


def test_sort_dir_asc_reverses(db_session):
    _seed_sortable(db_session)
    assert _order(db_session, sort_by="tokens", sort_dir="asc") == ["short", "long", "mid"]
    assert _order(db_session, sort_by="duration", sort_dir="asc") == ["short", "mid", "long"]


def test_sort_recent_default_backcompat(db_session):
    _seed_sortable(db_session)
    # 'recent' orders by ts_end (all share _NOW), tiebroken by session_id — but
    # the important contract is that the legacy values still resolve and page.
    res = query_sessions_paginated(db_session, **_kwargs(), sort_by="recent")
    assert res["total"] == 3
    assert {s["session_id"] for s in res["sessions"]} == {"short", "mid", "long"}


def test_project_filter(db_session):
    _add(db_session, session_id="a", project="runway")
    _add(db_session, session_id="b", project="sanctuary")
    _add(db_session, session_id="c", project="runway")

    res = query_sessions_paginated(db_session, **_kwargs(), project="runway")
    assert res["total"] == 2
    assert {s["session_id"] for s in res["sessions"]} == {"a", "c"}
