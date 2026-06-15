"""Tests for query_top_models — cross-provider model ranking."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.queries.top_models import query_top_models


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
    session: Session,
    *,
    event_id: str,
    provider_id: str,
    model_id: str | None,
    ts: datetime = _NOW,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    cost_usd: float = 0.0,
    cost_cache_read: float = 0.0,
    cost_cache_create: float = 0.0,
    kind: str = "message",
) -> None:
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id="acc1",
            event_id=event_id,
            model_id=model_id,
            session_id="s1",
            ts=ts,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_cache_read=tokens_cache_read,
            tokens_cache_create=tokens_cache_create,
            tokens_reasoning=tokens_reasoning,
            cost_usd=cost_usd,
            cost_cache_read=cost_cache_read,
            cost_cache_create=cost_cache_create,
            sidecar_id="local",
            kind=kind,
        )
    )
    session.commit()


def test_ranks_by_tokens_descending(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", model_id="opus", tokens_input=1000)
    _add(db_session, event_id="b", provider_id="gemini", model_id="flash", tokens_input=3000)
    _add(db_session, event_id="c", provider_id="chatgpt", model_id="gpt", tokens_input=2000)

    rows = query_top_models(db_session, since=_SINCE, metric="tokens")

    assert [r["model_id"] for r in rows] == ["flash", "gpt", "opus"]
    assert [r["tokens_total"] for r in rows] == [3000, 2000, 1000]


def test_ranks_by_cost_descending(db_session):
    _add(db_session, event_id="a", provider_id="anthropic", model_id="opus", cost_usd=5.0)
    _add(db_session, event_id="b", provider_id="gemini", model_id="flash", cost_usd=1.0)

    rows = query_top_models(db_session, since=_SINCE, metric="cost")

    assert [r["model_id"] for r in rows] == ["opus", "flash"]


def test_collapses_same_model_across_providers(db_session):
    # Same model_id seen under two providers must merge into one ranked row
    # that lists both contributing providers.
    _add(db_session, event_id="a", provider_id="anthropic", model_id="sonnet", tokens_input=100)
    _add(db_session, event_id="b", provider_id="opencode", model_id="sonnet", tokens_input=400)

    rows = query_top_models(db_session, since=_SINCE, metric="tokens")

    assert len(rows) == 1
    assert rows[0]["model_id"] == "sonnet"
    assert rows[0]["tokens_total"] == 500
    assert sorted(rows[0]["providers"]) == ["anthropic", "opencode"]


def test_exclude_cache_changes_sort(db_session):
    # "cheap" is mostly cache; "real" is mostly fresh I/O. Excluding cache must
    # flip the order and the reported sort metric.
    _add(
        db_session,
        event_id="cheap",
        provider_id="anthropic",
        model_id="cheap",
        tokens_input=100,
        tokens_cache_read=10_000,
    )
    _add(
        db_session,
        event_id="real",
        provider_id="anthropic",
        model_id="real",
        tokens_input=5_000,
        tokens_output=2_000,
    )

    incl = query_top_models(db_session, since=_SINCE, metric="tokens", exclude_cache=False)
    assert incl[0]["model_id"] == "cheap"

    excl = query_top_models(db_session, since=_SINCE, metric="tokens", exclude_cache=True)
    assert excl[0]["model_id"] == "real"


def test_excludes_errors_and_null_models_and_respects_range(db_session):
    _add(db_session, event_id="ok", provider_id="anthropic", model_id="opus", tokens_input=100)
    _add(
        db_session,
        event_id="err",
        provider_id="anthropic",
        model_id="opus",
        tokens_input=999,
        kind="error",
    )
    _add(db_session, event_id="nomodel", provider_id="anthropic", model_id=None, tokens_input=999)
    _add(
        db_session,
        event_id="old",
        provider_id="anthropic",
        model_id="opus",
        ts=_NOW - timedelta(days=10),
        tokens_input=999,
    )

    rows = query_top_models(db_session, since=_SINCE, metric="tokens")

    assert len(rows) == 1
    assert rows[0]["model_id"] == "opus"
    assert rows[0]["tokens_total"] == 100  # only the in-range message row


def test_limit_caps_rows(db_session):
    for i in range(5):
        _add(
            db_session,
            event_id=f"m{i}",
            provider_id="anthropic",
            model_id=f"model-{i}",
            tokens_input=i + 1,
        )

    rows = query_top_models(db_session, since=_SINCE, metric="tokens", limit=2)
    assert len(rows) == 2
    assert [r["model_id"] for r in rows] == ["model-4", "model-3"]
