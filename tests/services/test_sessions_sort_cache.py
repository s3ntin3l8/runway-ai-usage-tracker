"""The cached, Python-sorted Sessions browser path against the all-SQL reference."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core import cache as cache_mod
from app.models.db import UsageEvent
from app.services.queries import sessions as sessions_mod
from app.services.queries.sessions import (
    SessionFilters,
    _index_cache_key,
    query_sessions_paginated,
)

_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
_SINCE = _NOW - timedelta(days=2)
_SORTS = ["recent", "tokens", "duration", "messages", "cost"]


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        yield s


def _add(
    s,
    sid,
    n,
    *,
    tokens=100,
    cost=0.5,
    start=0,
    span=0,
    account="acc1",
    model="opus",
    turn=False,
):
    """`n` events for session `sid` spanning `span` minutes from `start` minutes back."""
    for i in range(n):
        offset = start - (span * i // max(n - 1, 1))
        s.add(
            UsageEvent(
                provider_id="anthropic",
                account_id=account,
                event_id=f"{account}-{sid}-{i}",
                session_id=sid,
                ts=_NOW - timedelta(minutes=offset),
                tokens_input=tokens,
                cost_usd=cost,
                model_id=model,
                turn_id=f"t{i // 2}" if turn else None,
                kind="message",
            )
        )
    s.commit()


def _seed_ties(s):
    # Ties on every key: equal tokens/cost/messages, same ts_end, same duration.
    for sid in ["s-b", "s-a", "s-c", "s-d"]:
        _add(s, sid, 3, tokens=50, cost=1.0, start=10, span=4)
    # Distinct values mixed in, including a zero-cost and a one-event session.
    _add(s, "s-e", 1, tokens=900, cost=0.0, start=1)
    _add(s, "s-f", 6, tokens=10, cost=7.0, start=30, span=20, turn=True)
    _add(s, "s-g", 2, tokens=50, cost=1.0, start=10, span=4)
    _add(s, "other", 2, account="acc2")


def _kw(**extra):
    return {"provider_id": "anthropic", "account_id": "acc1", "since": _SINCE, **extra}


@pytest.mark.parametrize("sort_by", _SORTS)
@pytest.mark.parametrize("sort_dir", ["asc", "desc"])
@pytest.mark.parametrize("page_size", [3, 25])
def test_cached_path_matches_sql_path(db, sort_by, sort_dir, page_size):
    _seed_ties(db)
    pages = 8 // page_size + 1
    for page in range(pages):
        args = {"page": page, "page_size": page_size, "sort_by": sort_by, "sort_dir": sort_dir}
        fast = query_sessions_paginated(db, **_kw(), **args)
        ref = query_sessions_paginated(db, **_kw(), **args, use_cache=False)
        assert fast == ref


def test_matches_sql_path_with_filters(db):
    _seed_ties(db)
    _add(db, "s-h", 2, model="sonnet")
    for extra in ({"model": "sonnet"}, {"project": "__none__"}, {"exclude_subagents": True}):
        for sort_by in _SORTS:
            args = {"page_size": 4, "sort_by": sort_by, "sort_dir": "asc", **extra}
            assert query_sessions_paginated(db, **_kw(), **args) == query_sessions_paginated(
                db, **_kw(), **args, use_cache=False
            )


def test_sort_handles_null_keys_like_sqlite():
    # NULL sorts below every value in SQLite: first ASC, last DESC.
    index = [
        ("a", "2026", 1, 5, None, 1.0),
        ("b", "2026", 1, 5, 2.0, 1.0),
        ("c", "2026", 1, 5, 1.0, 1.0),
    ]
    assert [r[0] for r in sessions_mod._sort_index(index, "cost", "asc")] == ["a", "c", "b"]
    assert [r[0] for r in sessions_mod._sort_index(index, "cost", "desc")] == ["b", "c", "a"]


def _count_selects(engine):
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _hook(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    return statements


def test_sort_and_page_changes_do_not_reaggregate(db, engine):
    _seed_ties(db)
    statements = _count_selects(engine)

    def window_scans():
        return sum(1 for q in statements if "GROUP BY session_id" in q and "julianday" in q)

    query_sessions_paginated(db, **_kw(), sort_by="recent", page_size=3)
    assert window_scans() == 1
    for sort_by in _SORTS:
        for page in (0, 1, 2):
            query_sessions_paginated(
                db, **_kw(), sort_by=sort_by, sort_dir="asc", page=page, page_size=3
            )
    assert window_scans() == 1
    # The reference path never scans for the index and issues its own count query.
    assert not any("COUNT(DISTINCT session_id)" in q for q in statements)


def test_window_edges_in_the_same_minute_share_an_entry(db, engine):
    _seed_ties(db)
    statements = _count_selects(engine)
    for seconds in (5, 40):
        query_sessions_paginated(
            db,
            provider_id="anthropic",
            account_id="acc1",
            since=_SINCE + timedelta(seconds=seconds),
            until=_NOW + timedelta(seconds=seconds),
        )
    assert sum(1 for q in statements if "julianday" in q) == 1


def test_cache_key_differs_per_filter_set_and_account():
    base = SessionFilters(provider_id="anthropic", account_id="acc1", since=_SINCE)
    variants = [
        base,
        SessionFilters(provider_id="anthropic", account_id="acc2", since=_SINCE),
        SessionFilters(provider_id="openai", account_id="acc1", since=_SINCE),
        SessionFilters(provider_id="anthropic", account_id="acc1", since=_SINCE, project="p"),
        SessionFilters(provider_id="anthropic", account_id="acc1", since=_SINCE, model="m"),
        SessionFilters(provider_id="anthropic", account_id="acc1", since=_SINCE, branch="b"),
        SessionFilters(provider_id="anthropic", account_id="acc1", since=_SINCE, sidecar_id="x"),
        SessionFilters(
            provider_id="anthropic", account_id="acc1", since=_SINCE, exclude_subagents=True
        ),
        SessionFilters(provider_id="anthropic", account_id="acc1", since=_SINCE, until=_NOW),
        SessionFilters(
            provider_id="anthropic", account_id="acc1", since=_SINCE - timedelta(days=1)
        ),
    ]
    assert len({_index_cache_key(v) for v in variants}) == len(variants)


def test_accounts_do_not_share_cached_totals(db):
    _seed_ties(db)
    one = query_sessions_paginated(db, **_kw())
    two = query_sessions_paginated(db, **{**_kw(), "account_id": "acc2"})
    assert one["total"] == 7
    assert two["total"] == 1


def test_entries_are_served_until_cleared(db, monkeypatch):
    _seed_ties(db)
    query_sessions_paginated(db, **_kw())
    _add(db, "late", 1, start=0)
    assert query_sessions_paginated(db, **_kw())["total"] == 7  # served from the cache
    cache_mod.cache_clear()
    assert query_sessions_paginated(db, **_kw())["total"] == 8


def test_endpoint_runs_the_query_off_the_event_loop(db, monkeypatch):
    from app.api.endpoints import usage as usage_mod
    from app.core.db import get_session
    from app.main import app
    from tests.fixtures.test_client import TestClient

    _seed_ties(db)
    seen: list[int] = []
    real = usage_mod.query_sessions_paginated

    def spy(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(usage_mod, "query_sessions_paginated", spy)
    app.dependency_overrides[get_session] = lambda: db
    try:
        resp = TestClient(app).get(
            "/api/v1/usage/sessions/paginated",
            params={"provider_id": "anthropic", "account_id": "acc1", "since": _SINCE.isoformat()},
        )
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert resp.status_code == 200
    assert resp.json()["total"] == 7
    assert seen
    assert threading.get_ident() not in seen


def test_deferred_index_creation_is_idempotent(engine):
    from app.core.db import _DEFERRED_INDEXES, _add_indexes_if_missing

    with engine.connect() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_usage_events_account_session_ts"))
        conn.commit()
        _add_indexes_if_missing(conn)
        _add_indexes_if_missing(conn)
        cols = [
            r[2]
            for r in conn.execute(text("PRAGMA index_info('ix_usage_events_account_session_ts')"))
        ]
    assert cols == ["provider_id", "account_id", "session_id", "ts"]
    assert "ix_usage_events_account_session_ts" in {name for name, _, _ in _DEFERRED_INDEXES}


def test_session_lookup_uses_the_new_index(db):
    plan = db.exec(  # type: ignore[call-overload]
        text(
            "EXPLAIN QUERY PLAN SELECT model_id FROM usage_events WHERE provider_id='p' "
            "AND account_id='a' AND session_id='s' AND ts >= '2026-01-01' GROUP BY model_id"
        )
    ).all()
    assert "ix_usage_events_account_session_ts" in " ".join(str(r) for r in plan)


def test_boundary_session_never_splits_total_from_the_page(db):
    # A session that exists only in the sliver between two same-minute requests.
    since = _NOW.replace(second=0, microsecond=0) - timedelta(hours=1)
    for i in range(4):
        _add(db, f"in-{i}", 1, start=10 + i)
    db.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc1",
            event_id="edge",
            session_id="edge",
            ts=since + timedelta(seconds=20),
            kind="message",
        )
    )
    db.commit()
    base = {"provider_id": "anthropic", "account_id": "acc1", "page_size": 3}
    first = query_sessions_paginated(db, since=since + timedelta(seconds=20), **base)
    second = query_sessions_paginated(db, since=since + timedelta(seconds=40), **base)
    assert first["total"] == second["total"] == 5
    assert len(second["sessions"]) == 3


def test_non_aligned_until_keeps_the_newest_events(db):
    until = _NOW.replace(second=45)
    db.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc1",
            event_id="newest",
            session_id="newest",
            ts=_NOW.replace(second=30),
            kind="message",
        )
    )
    db.commit()
    kwargs = {"provider_id": "anthropic", "account_id": "acc1", "since": _SINCE, "until": until}
    fast = query_sessions_paginated(db, **kwargs)
    ref = query_sessions_paginated(db, **kwargs, use_cache=False)
    assert fast == ref
    assert fast["total"] == 1
