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


# ---------------------------------------------------------------------------
# Model / branch / subagent / unattributed-project filters
# ---------------------------------------------------------------------------


def _ev(
    session, n, *, session_id, model="opus", branch=None, project=None, subagent=None, tokens=100
):
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc1",
            event_id=f"{session_id}-{n}",
            session_id=session_id,
            project=project,
            git_branch=branch,
            subagent_type=subagent,
            ts=_NOW - timedelta(minutes=n),
            tokens_input=tokens,
            model_id=model,
            sidecar_id="local",
            kind="message",
        )
    )
    session.commit()


def _seed_filters(session):
    # s1: opus on main (project runway) + sonnet subagent on feat
    _ev(session, 1, session_id="s1", model="opus", branch="main", project="runway", tokens=100)
    _ev(
        session,
        2,
        session_id="s1",
        model="sonnet",
        branch="feat",
        project="runway",
        subagent="Explore",
        tokens=40,
    )
    # s2: opus on main, no project
    _ev(session, 3, session_id="s2", model="opus", branch="main", project=None, tokens=10)
    # s3: sonnet on feat, empty project
    _ev(session, 4, session_id="s3", model="sonnet", branch="feat", project="", tokens=20)


def _ids(res):
    return {s["session_id"] for s in res["sessions"]}


def _consistent(res):
    assert res["total"] == len(res["sessions"])


def test_model_filter_narrows_rows_total_and_breakdown(db_session):
    _seed_filters(db_session)
    res = query_sessions_paginated(db_session, **_kwargs(), model="sonnet")
    assert _ids(res) == {"s1", "s3"}
    _consistent(res)
    s1 = next(s for s in res["sessions"] if s["session_id"] == "s1")
    assert s1["tokens_total"] == 40
    assert [m["model_id"] for m in s1["by_model"]] == ["sonnet"]
    assert s1["models"] == ["sonnet"]


def test_branch_filter(db_session):
    _seed_filters(db_session)
    res = query_sessions_paginated(db_session, **_kwargs(), branch="main")
    assert _ids(res) == {"s1", "s2"}
    _consistent(res)
    s1 = next(s for s in res["sessions"] if s["session_id"] == "s1")
    assert s1["tokens_total"] == 100


def test_subagents_exclude_drops_exactly_the_subagent_events(db_session):
    _seed_filters(db_session)
    base = query_sessions_paginated(db_session, **_kwargs())
    res = query_sessions_paginated(db_session, **_kwargs(), exclude_subagents=True)
    b1 = next(s for s in base["sessions"] if s["session_id"] == "s1")
    r1 = next(s for s in res["sessions"] if s["session_id"] == "s1")
    assert b1["tokens_total"] - r1["tokens_total"] == 40
    assert b1["subagents"] and r1["subagents"] == []
    assert r1["subagent_msgs"] == 0
    assert [m["model_id"] for m in r1["by_model"]] == ["opus"]
    # sessions without subagent events are untouched
    assert next(s for s in res["sessions"] if s["session_id"] == "s2")["tokens_total"] == 10
    assert res["total"] == base["total"] == 3


def test_subagents_exclude_drops_sessions_made_only_of_subagent_events(db_session):
    _ev(db_session, 1, session_id="only-sub", subagent="Explore")
    _ev(db_session, 2, session_id="main", subagent=None)
    res = query_sessions_paginated(db_session, **_kwargs(), exclude_subagents=True)
    assert _ids(res) == {"main"}
    _consistent(res)


def test_no_project_sentinel_selects_null_and_empty(db_session):
    _seed_filters(db_session)
    res = query_sessions_paginated(db_session, **_kwargs(), project="__none__")
    assert _ids(res) == {"s2", "s3"}
    _consistent(res)


def test_no_project_sentinel_is_event_level(db_session):
    _ev(db_session, 1, session_id="mixed", project="runway", tokens=100)
    _ev(db_session, 2, session_id="mixed", project=None, tokens=7)
    res = query_sessions_paginated(db_session, **_kwargs(), project="__none__")
    assert _ids(res) == {"mixed"}
    assert res["sessions"][0]["tokens_total"] == 7


def test_real_project_name_keeps_its_meaning(db_session):
    _seed_filters(db_session)
    res = query_sessions_paginated(db_session, **_kwargs(), project="runway")
    assert _ids(res) == {"s1"}


def test_filters_intersect(db_session):
    _seed_filters(db_session)
    res = query_sessions_paginated(
        db_session, **_kwargs(), model="opus", branch="main", project="__none__"
    )
    assert _ids(res) == {"s2"}
    _consistent(res)
    res = query_sessions_paginated(db_session, **_kwargs(), model="sonnet", branch="main")
    assert res["sessions"] == [] and res["total"] == 0
    res = query_sessions_paginated(db_session, **_kwargs(), model="sonnet", exclude_subagents=True)
    assert _ids(res) == {"s3"}


def test_no_filters_leaves_results_unchanged(db_session):
    _seed_filters(db_session)
    res = query_sessions_paginated(db_session, **_kwargs())
    assert _ids(res) == {"s1", "s2", "s3"}
    s1 = next(s for s in res["sessions"] if s["session_id"] == "s1")
    assert s1["tokens_total"] == 140
    assert len(s1["subagents"]) == 1


def test_project_filter_applies_to_breakdowns(db_session):
    # The breakdowns used to ignore `project`, so a filtered row described
    # events outside the filter.
    _ev(db_session, 1, session_id="x", model="opus", project="runway", tokens=100)
    _ev(
        db_session,
        2,
        session_id="x",
        model="sonnet",
        project="other",
        subagent="Explore",
        tokens=50,
    )
    row = query_sessions(db_session, **_kwargs(), project="runway")[0]
    assert row["tokens_total"] == 100
    assert [m["model_id"] for m in row["by_model"]] == ["opus"]
    assert row["subagents"] == []
    assert sum(m["tokens_total"] for m in row["by_model"]) == row["tokens_total"]


def test_session_filters_where_uses_only_bound_values():
    from app.services.queries.sessions import SessionFilters

    f = SessionFilters(
        provider_id="p",
        account_id="a",
        since=_SINCE,
        project="__none__",
        model="x'; DROP TABLE usage_events; --",
        branch="b",
        exclude_subagents=True,
    )
    assert "DROP" not in f.where()
    assert "project" not in f.params()
    assert f.params()["model"].startswith("x'")
    assert f.cache_key() == SessionFilters(**{**f.__dict__}).cache_key()


def test_filter_options_rank_search_and_exclude_blanks(db_session):
    from app.services.queries.sessions import query_session_filter_options

    for i in range(3):
        _ev(db_session, i, session_id="a", model="opus", branch="main")
    _ev(db_session, 5, session_id="b", model="sonnet", branch="feat_x")
    _ev(db_session, 6, session_id="b", model="", branch="")
    opts = lambda kind, **kw: query_session_filter_options(  # noqa: E731
        db_session, kind=kind, provider_id="anthropic", account_id="acc1", since=_SINCE, **kw
    )
    assert opts("model") == ["opus", "sonnet"]
    assert opts("branch") == ["main", "feat_x"]
    assert opts("branch", limit=1) == ["main"]
    assert opts("model", search="SON") == ["sonnet"]
    assert opts("branch", search="_x") == ["feat_x"]
    assert opts("branch", search="%") == []
    assert opts("model", until=_NOW - timedelta(minutes=4)) == ["sonnet"]


def test_filter_options_follow_the_active_project(db_session):
    from app.services.queries.sessions import query_session_filter_options

    _ev(db_session, 1, session_id="a", model="opus", branch="main", project="runway")
    _ev(db_session, 2, session_id="b", model="sonnet", branch="feat", project="other")
    _ev(db_session, 3, session_id="c", model="haiku", branch="x", project=None)
    kw = {"provider_id": "anthropic", "account_id": "acc1", "since": _SINCE}
    assert query_session_filter_options(db_session, kind="model", project="runway", **kw) == [
        "opus"
    ]
    assert query_session_filter_options(db_session, kind="branch", project="__none__", **kw) == [
        "x"
    ]
