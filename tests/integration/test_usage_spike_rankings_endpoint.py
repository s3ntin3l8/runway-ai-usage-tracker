"""The ``/usage/top-sessions`` and ``/usage/top-hosts`` endpoints.

The two rankings that answer "what drove that spike": which session, and
which host. The queries themselves are covered in
``tests/services/test_query_top_sessions.py`` and
``tests/services/test_query_top_hosts.py``; this covers the wiring the query
alone can't — the response model, the range parameters (including the
one-day rolling window the "Last 24 hours" quick range asks for), and the
cache.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.cache import cache_clear
from app.core.db import get_session
from app.main import app
from app.models.db import SessionName, UsageEvent


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
    provider_id: str = "anthropic",
    account_id: str = "acc-1",
    sidecar_id: str = "host-a",
    session_id: str | None = "s1",
    project: str | None = "alpha",
    model_id: str | None = "opus-4.8",
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
            model_id=model_id,
            session_id=session_id,
            project=project,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=cost_usd,
        )
    )
    session.commit()


def _client() -> TestClient:
    return TestClient(app)


def test_top_sessions_endpoint_ranks_the_sessions(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, session_id="s1", tokens_input=1000)
    _event(session, "e2", now, session_id="s2", tokens_input=10)

    resp = _client().get("/api/v1/usage/top-sessions?days=1")
    assert resp.status_code == 200
    body = resp.json()
    assert [s["session_id"] for s in body["sessions"]] == ["s1", "s2"]
    assert body["metric"] == "tokens"
    assert "generated_at" in body


def test_top_sessions_endpoint_reports_the_session_context(session):
    """The row carries what identifies the session: project, host, providers."""
    now = datetime.now(UTC)
    _event(session, "e1", now, session_id="s1", project="alpha", sidecar_id="workstation")

    entry = _client().get("/api/v1/usage/top-sessions?days=1").json()["sessions"][0]
    assert entry["session_id"] == "s1"
    assert entry["project"] == "alpha"
    assert entry["sidecar_id"] == "workstation"
    assert entry["providers"] == ["anthropic"]
    assert entry["models"] == ["opus-4.8"]
    assert entry["tokens_total"] == 110
    assert entry["ts_start"] is not None


def _name(session: Session, provider: str, sid: str, name: str, kind: str = "agent") -> None:
    session.add(SessionName(provider_id=provider, session_id=sid, name=name, kind=kind))
    session.commit()


def _names_by_id() -> dict[str, str | None]:
    body = _client().get("/api/v1/usage/top-sessions?days=1").json()
    return {s["session_id"]: s["name"] for s in body["sessions"]}


def test_top_sessions_name_prefers_agent_and_is_null_without_one(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, session_id="s1", tokens_input=1000)
    _event(session, "e2", now, session_id="s2", tokens_input=10)
    _name(session, "anthropic", "s1", "generated one", kind="generated")
    _name(session, "anthropic", "s1", "agent one", kind="agent")

    assert _names_by_id() == {"s1": "agent one", "s2": None}


def test_top_sessions_shared_id_is_one_row_and_gets_the_name(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, provider_id="anthropic", session_id="s1")
    _event(session, "e2", now, provider_id="openai", session_id="s1")
    _name(session, "openai", "s1", "only openai")

    body = _client().get("/api/v1/usage/top-sessions?days=1").json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["name"] == "only openai"


def test_top_sessions_alphabetically_first_provider_name_wins(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, provider_id="openai", session_id="s1")
    _event(session, "e2", now, provider_id="anthropic", session_id="s1")
    _name(session, "openai", "s1", "from openai")
    _name(session, "anthropic", "s1", "from anthropic")

    assert _names_by_id() == {"s1": "from anthropic"}


def test_top_sessions_name_queries_are_bounded_by_providers(session, monkeypatch):
    from app.api.endpoints import usage_rankings

    now = datetime.now(UTC)
    for i in range(6):
        _event(session, f"e{i}", now, provider_id="anthropic", session_id=f"s{i}")
    _event(session, "eo", now, provider_id="openai", session_id="s0")
    calls: list[tuple[str, list[str]]] = []
    real = usage_rankings.resolve_names

    def spy(sess, provider_id, session_ids):
        calls.append((provider_id, list(session_ids)))
        return real(sess, provider_id, session_ids)

    monkeypatch.setattr(usage_rankings, "resolve_names", spy)
    _client().get("/api/v1/usage/top-sessions?days=1")

    assert sorted(p for p, _ in calls) == ["anthropic", "openai"]
    assert dict(calls)["openai"] == ["s0"]


def test_top_sessions_endpoint_one_day_window_is_the_last_24_hours(session):
    """``days=1`` is the rolling window the "Last 24 hours" quick range sends."""
    now = datetime.now(UTC)
    _event(session, "recent", now - timedelta(hours=2), session_id="s-recent")
    _event(session, "old", now - timedelta(hours=30), session_id="s-old")

    body = _client().get("/api/v1/usage/top-sessions?days=1").json()
    assert [s["session_id"] for s in body["sessions"]] == ["s-recent"]


def test_top_sessions_endpoint_since_and_until_win_over_days(session):
    now = datetime.now(UTC)
    _event(session, "e1", now - timedelta(days=5), session_id="s-old")
    _event(session, "e2", now - timedelta(hours=1), session_id="s-new")

    body = (
        _client()
        .get(
            "/api/v1/usage/top-sessions",
            params={
                "days": 30,
                "since": (now - timedelta(hours=2)).isoformat(),
                "until": now.isoformat(),
            },
        )
        .json()
    )
    assert [s["session_id"] for s in body["sessions"]] == ["s-new"]


def test_top_sessions_endpoint_rejects_an_unknown_metric(session):
    resp = _client().get("/api/v1/usage/top-sessions?days=1&metric=bananas")
    assert resp.status_code == 422


def test_top_sessions_endpoint_second_call_is_served_from_cache(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, session_id="s1")

    client = _client()
    assert len(client.get("/api/v1/usage/top-sessions?days=1").json()["sessions"]) == 1

    _event(session, "e2", now, session_id="s2")
    assert len(client.get("/api/v1/usage/top-sessions?days=1").json()["sessions"]) == 1

    cache_clear()
    assert len(client.get("/api/v1/usage/top-sessions?days=1").json()["sessions"]) == 2


def test_top_sessions_endpoint_does_not_share_a_cache_entry_with_another_window(session):
    """Two different windows must not answer each other's cached payload."""
    now = datetime.now(UTC)
    _event(session, "recent", now - timedelta(hours=1), session_id="s-recent")
    _event(session, "old", now - timedelta(days=5), session_id="s-old")

    client = _client()
    one_day = client.get("/api/v1/usage/top-sessions?days=1").json()["sessions"]
    seven_days = client.get("/api/v1/usage/top-sessions?days=7").json()["sessions"]

    assert [s["session_id"] for s in one_day] == ["s-recent"]
    assert {s["session_id"] for s in seven_days} == {"s-recent", "s-old"}


def test_top_hosts_endpoint_ranks_the_hosts(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, sidecar_id="host-a", session_id="s1", tokens_input=1000)
    _event(session, "e2", now, sidecar_id="host-b", session_id="s2", tokens_input=10)

    resp = _client().get("/api/v1/usage/top-hosts?days=1")
    assert resp.status_code == 200
    body = resp.json()
    assert [h["sidecar_id"] for h in body["hosts"]] == ["host-a", "host-b"]
    assert body["metric"] == "tokens"
    assert "generated_at" in body


def test_top_hosts_endpoint_counts_the_sessions_per_host(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, sidecar_id="host-a", session_id="s1")
    _event(session, "e2", now, sidecar_id="host-a", session_id="s2")

    entry = _client().get("/api/v1/usage/top-hosts?days=1").json()["hosts"][0]
    assert entry["sidecar_id"] == "host-a"
    assert entry["sessions"] == 2
    assert entry["providers"] == ["anthropic"]


def test_top_hosts_endpoint_one_day_window_is_the_last_24_hours(session):
    now = datetime.now(UTC)
    _event(session, "recent", now - timedelta(hours=2), sidecar_id="host-recent")
    _event(session, "old", now - timedelta(hours=30), sidecar_id="host-old")

    body = _client().get("/api/v1/usage/top-hosts?days=1").json()
    assert [h["sidecar_id"] for h in body["hosts"]] == ["host-recent"]


def test_top_hosts_endpoint_filters_by_provider(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, provider_id="anthropic", sidecar_id="host-a")
    _event(session, "e2", now, provider_id="gemini", sidecar_id="host-b")

    body = _client().get("/api/v1/usage/top-hosts?days=1&provider_id=anthropic").json()
    assert [h["sidecar_id"] for h in body["hosts"]] == ["host-a"]


def test_top_hosts_endpoint_filters_by_host(session):
    """The source filter narrows the ranking, like it does for its siblings."""
    now = datetime.now(UTC)
    _event(session, "e1", now, sidecar_id="host-a")
    _event(session, "e2", now, sidecar_id="host-b")

    body = _client().get("/api/v1/usage/top-hosts?days=1&sidecar_id=host-b").json()
    assert [h["sidecar_id"] for h in body["hosts"]] == ["host-b"]


def test_top_hosts_endpoint_rejects_an_unknown_metric(session):
    resp = _client().get("/api/v1/usage/top-hosts?days=1&metric=bananas")
    assert resp.status_code == 422
