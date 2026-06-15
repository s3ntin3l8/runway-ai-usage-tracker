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


def test_project_filter(db_session):
    _add(db_session, session_id="a", project="runway")
    _add(db_session, session_id="b", project="sanctuary")
    _add(db_session, session_id="c", project="runway")

    res = query_sessions_paginated(db_session, **_kwargs(), project="runway")
    assert res["total"] == 2
    assert {s["session_id"] for s in res["sessions"]} == {"a", "c"}
