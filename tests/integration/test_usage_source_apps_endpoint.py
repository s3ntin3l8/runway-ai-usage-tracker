"""The ``/usage/source-apps`` endpoint — the per-client-app ranking.

The endpoint is the HTTP face of the ``source_app`` dimension: which client
app spent the quota, which the provider id alone can't say, because several
apps share one provider and the canonical-provider retag folds them together.

The query itself is covered in ``tests/unit/test_source_app.py``; this covers
the wiring the query alone can't — the response model, the query parameters,
the cache, and the ``/usage/sources`` list that feeds the UI's app picker.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.cache import cache_clear
from app.core.db import get_session
from app.main import app
from app.models.db import UsageEvent


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        app.dependency_overrides[get_session] = lambda: s
        cache_clear()
        yield s
        cache_clear()
        app.dependency_overrides.pop(get_session, None)


def _event(
    session: Session,
    event_id: str,
    ts: datetime,
    *,
    source_app: str | None,
    provider_id: str = "minimax",
    account_id: str = "acc-1",
    sidecar_id: str = "dev-01",
    tokens_input: int = 100,
    tokens_output: int = 10,
    cost_usd: float = 0.01,
) -> None:
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
            event_id=event_id,
            ts=ts,
            kind="message",
            model_id="MiniMax-M3",
            source_app=source_app,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=cost_usd,
        )
    )
    session.commit()


def _client() -> TestClient:
    return TestClient(app)


def test_source_apps_endpoint_ranks_the_client_apps(session):
    """The ranking is by tokens by default, and the busiest app comes first."""
    now = datetime.now(UTC)
    _event(session, "e1", now, source_app="minimax_code", tokens_input=1000)
    _event(session, "e2", now, source_app="opencode", tokens_input=10)

    resp = _client().get("/api/v1/usage/source-apps?days=1")
    assert resp.status_code == 200
    body = resp.json()
    assert [a["source_app"] for a in body["apps"]] == ["minimax_code", "opencode"]
    assert body["metric"] == "tokens"
    assert "generated_at" in body


def test_source_apps_endpoint_reports_the_totals_per_app(session):
    """Each entry carries the totals the chart needs, not just a name."""
    now = datetime.now(UTC)
    _event(
        session,
        "e1",
        now,
        source_app="minimax_code",
        tokens_input=1000,
        tokens_output=100,
        cost_usd=0.25,
    )

    entry = _client().get("/api/v1/usage/source-apps?days=1").json()["apps"][0]
    assert entry["source_app"] == "minimax_code"
    assert entry["msgs"] == 1
    assert entry["tokens_input"] == 1000
    assert entry["tokens_output"] == 100
    assert entry["tokens_total"] == 1100
    assert entry["cost_usd"] == 0.25
    # The providers the app's usage was billed to — several apps can share one.
    assert entry["providers"] == ["minimax"]


def test_source_apps_endpoint_filters_by_provider_and_account(session):
    """The filters narrow the ranking the same way the sibling endpoints do."""
    now = datetime.now(UTC)
    _event(session, "e1", now, source_app="minimax_code")
    _event(
        session,
        "e2",
        now,
        source_app="claude_code",
        provider_id="anthropic",
        account_id="other-acc",
    )

    body = _client().get("/api/v1/usage/source-apps?days=1&provider_id=minimax").json()
    assert [a["source_app"] for a in body["apps"]] == ["minimax_code"]

    body = _client().get("/api/v1/usage/source-apps?days=1&account_id=acc-1").json()
    assert [a["source_app"] for a in body["apps"]] == ["minimax_code"]


def test_source_apps_endpoint_cost_metric_sorts_by_cost(session):
    """``metric=cost`` ranks by spend instead of by tokens."""
    now = datetime.now(UTC)
    _event(session, "e1", now, source_app="minimax_code", tokens_input=1000, cost_usd=0.01)
    _event(session, "e2", now, source_app="opencode", tokens_input=10, cost_usd=5.0)

    body = _client().get("/api/v1/usage/source-apps?days=1&metric=cost").json()
    assert body["metric"] == "cost"
    assert [a["source_app"] for a in body["apps"]] == ["opencode", "minimax_code"]


def test_source_apps_endpoint_second_call_is_served_from_cache(session):
    """A cache hit replays the payload instead of recomputing it."""
    now = datetime.now(UTC)
    _event(session, "e1", now, source_app="minimax_code")

    client = _client()
    first = client.get("/api/v1/usage/source-apps?days=1")
    assert len(first.json()["apps"]) == 1

    _event(session, "e2", now, source_app="opencode")
    second = client.get("/api/v1/usage/source-apps?days=1")
    assert len(second.json()["apps"]) == 1, "expected the cached payload"

    cache_clear()
    third = client.get("/api/v1/usage/source-apps?days=1")
    assert len(third.json()["apps"]) == 2


def test_usage_sources_lists_the_client_apps(session):
    """``/usage/sources`` feeds the UI's app picker, so it has to carry them."""
    now = datetime.now(UTC)
    _event(session, "e1", now, source_app="minimax_code")
    _event(session, "e2", now, source_app="opencode")

    body = _client().get("/api/v1/usage/sources").json()
    assert body["source_apps"] == ["minimax_code", "opencode"]
    assert body["sidecar_ids"] == ["dev-01"]


def test_usage_sources_omits_rows_without_a_source_app(session):
    """A row with no app (and no provider to fall back to) is not listed."""
    now = datetime.now(UTC)
    _event(session, "e1", now, source_app=None)

    body = _client().get("/api/v1/usage/sources").json()
    assert body["source_apps"] == []
