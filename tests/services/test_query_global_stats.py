"""Tests for query_global_stats — the cross-provider lifetime snapshot."""

import os
import tempfile
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.period_rollups import update_rollups_for_event
from app.services.queries.global_stats import query_global_stats


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


def _add(
    session: Session,
    *,
    event_id: str,
    provider_id: str = "anthropic",
    model_id: str | None = "opus",
    session_id: str | None = "s1",
    ts: datetime,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_cache_read: int = 0,
    cost_usd: float = 0.0,
    cost_cache_read: float = 0.0,
) -> None:
    ev = UsageEvent(
        provider_id=provider_id,
        account_id="acc1",
        event_id=event_id,
        model_id=model_id,
        session_id=session_id,
        ts=ts,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        cost_usd=cost_usd,
        cost_cache_read=cost_cache_read,
        sidecar_id="local",
        kind="message",
    )
    session.add(ev)
    session.flush()
    update_rollups_for_event(session, ev)
    session.commit()


def test_lifetime_totals_and_distinct_counts(db_session):
    _add(
        db_session,
        event_id="a",
        provider_id="anthropic",
        model_id="opus",
        ts=datetime(2026, 5, 1, 10, tzinfo=UTC),
        tokens_input=1000,
        tokens_output=500,
        cost_usd=2.0,
    )
    _add(
        db_session,
        event_id="b",
        provider_id="gemini",
        model_id="flash",
        ts=datetime(2026, 5, 2, 10, tzinfo=UTC),
        tokens_input=300,
        cost_usd=0.5,
    )

    stats = query_global_stats(db_session)

    assert stats["lifetime"]["tokens_total"] == 1800
    assert stats["lifetime"]["cost_usd"] == pytest.approx(2.5)
    assert stats["lifetime"]["msgs"] == 2
    assert stats["distinct_models"] == 2
    assert stats["distinct_providers"] == 2


def test_cache_hit_ratio(db_session):
    _add(
        db_session,
        event_id="a",
        ts=datetime(2026, 5, 1, 10, tzinfo=UTC),
        tokens_input=250,
        tokens_cache_read=750,
    )
    stats = query_global_stats(db_session)
    # cache_read / all tokens = 750 / 1000
    assert stats["cache_hit_ratio"] == pytest.approx(0.75)


def test_session_economics_use_per_session_subquery(db_session):
    # Two sessions; a session-less event must NOT dilute the averages.
    _add(
        db_session,
        event_id="s1a",
        session_id="s1",
        ts=datetime(2026, 5, 1, 10, tzinfo=UTC),
        tokens_input=100,
        cost_usd=1.0,
    )
    _add(
        db_session,
        event_id="s1b",
        session_id="s1",
        ts=datetime(2026, 5, 1, 11, tzinfo=UTC),
        tokens_input=100,
        cost_usd=1.0,
    )
    _add(
        db_session,
        event_id="s2a",
        session_id="s2",
        ts=datetime(2026, 5, 2, 10, tzinfo=UTC),
        tokens_input=400,
        cost_usd=2.0,
    )
    _add(
        db_session,
        event_id="apionly",
        session_id=None,
        ts=datetime(2026, 5, 3, 10, tzinfo=UTC),
        tokens_input=9999,
        cost_usd=99.0,
    )

    stats = query_global_stats(db_session)

    assert stats["sessions"]["count"] == 2
    # s1 = 200 tok / $2, s2 = 400 tok / $2 → avg 300 tok, $2
    assert stats["sessions"]["avg_tokens"] == pytest.approx(300.0)
    assert stats["sessions"]["avg_cost"] == pytest.approx(2.0)


def test_busiest_day_and_hour(db_session):
    # Day 2026-05-02 is the heavier day; 18:00 UTC is the heavier hour.
    _add(
        db_session,
        event_id="d1",
        ts=datetime(2026, 5, 1, 9, tzinfo=UTC),
        tokens_input=100,
    )
    _add(
        db_session,
        event_id="d2",
        ts=datetime(2026, 5, 2, 18, tzinfo=UTC),
        tokens_input=900,
    )

    stats = query_global_stats(db_session)

    assert stats["busiest_day"]["period_key"] == "2026-05-02"
    assert stats["busiest_hour"]["hour"] == 18  # UTC bucketing (tz=None)


def test_busiest_hour_respects_local_tz(db_session):
    # 23:00 UTC → 19:00 in America/New_York (EDT, UTC-4) in May.
    _add(
        db_session,
        event_id="late",
        ts=datetime(2026, 5, 2, 23, tzinfo=UTC),
        tokens_input=500,
    )
    stats = query_global_stats(db_session, tz=ZoneInfo("America/New_York"))
    assert stats["busiest_hour"]["hour"] == 19


def test_empty_db_returns_safe_zeros(db_session):
    stats = query_global_stats(db_session)
    assert stats["lifetime"]["tokens_total"] == 0
    assert stats["sessions"]["count"] == 0
    assert stats["cache_hit_ratio"] == 0.0
    assert stats["busiest_day"] is None
    assert stats["busiest_hour"] is None
