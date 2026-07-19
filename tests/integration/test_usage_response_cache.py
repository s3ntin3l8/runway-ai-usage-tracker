"""Integration tests for the TTL response cache wired into the read-heavy
usage endpoints (/fleet, /top-models, /top-projects, /top-tools,
/global-stats) — see app/core/cache.py and app/api/endpoints/usage.py.

Before this file, only the underlying query_* service functions had
coverage (tests/services/test_query_*.py); the endpoint-level wiring —
cache hit/miss branching, response model construction, and the
provider_id/window_type normalization used as the cache key — was
exercised by nothing at the HTTP layer.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.cache import cache_clear
from app.core.db import get_session
from app.main import app
from app.models.db import LatestUsage, UsageEvent, UsagePeriodRollup

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


def _client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    session: Session,
    event_id: str,
    ts: datetime,
    *,
    provider_id: str = "anthropic",
    account_id: str = "u@x.com",
    model_id: str = "sonnet",
    project: str | None = None,
    tools_json: str | None = None,
    session_id: str | None = None,
    tokens_input: int = 100,
    tokens_output: int = 50,
    cost_usd: float = 0.01,
) -> None:
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id="dev-01",
            event_id=event_id,
            ts=ts,
            kind="message",
            model_id=model_id,
            project=project,
            tools_json=tools_json,
            session_id=session_id,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=cost_usd,
        )
    )
    session.commit()


def _rollup(session: Session, *, provider_id: str = "anthropic", msgs: int = 5) -> None:
    session.add(
        UsagePeriodRollup(
            provider_id=provider_id,
            account_id="u@x.com",
            period_type="lifetime",
            period_key="all",
            model_id="",
            sidecar_id="",
            msgs=msgs,
            tokens_input=1000,
            tokens_output=500,
            cost_usd=1.5,
            last_updated=datetime.now(UTC),
        )
    )
    session.commit()


def _latest_usage(session: Session, *, provider_id: str = "anthropic") -> None:
    import json

    session.add(
        LatestUsage(
            provider_id=provider_id,
            account_id="u@x.com",
            sidecar_id="local",
            window_type="monthly",
            variant="default",
            card_json=json.dumps(
                {
                    "service_name": provider_id,
                    "provider_id": provider_id,
                    "account_id": "u@x.com",
                    "window_type": "monthly",
                    "unit_type": "tokens",
                    "used_value": 1.0,
                    "limit_value": 100.0,
                    "is_unlimited": False,
                    "health": "good",
                }
            ),
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# /fleet — cache hit actually serves the cached payload, not a recompute
# ---------------------------------------------------------------------------


def test_fleet_endpoint_second_call_is_served_from_cache(session):
    _latest_usage(session, provider_id="anthropic")

    client = _client()
    first = client.get("/api/v1/usage/fleet")
    assert first.status_code == 200
    assert len(first.json()["fleet"]) == 1

    # Mutate the underlying table directly — a cache miss would now see 0 cards.
    session.exec(delete(LatestUsage))
    session.commit()

    second = client.get("/api/v1/usage/fleet")
    assert len(second.json()["fleet"]) == 1, "expected the cached payload, not a fresh recompute"

    cache_clear()
    third = client.get("/api/v1/usage/fleet")
    assert third.json()["fleet"] == []


# ---------------------------------------------------------------------------
# /top-models
# ---------------------------------------------------------------------------


def test_top_models_endpoint_returns_ranked_models(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, model_id="opus", tokens_input=1000, tokens_output=0)
    _event(session, "e2", now, model_id="sonnet", tokens_input=10, tokens_output=0)

    resp = _client().get("/api/v1/usage/top-models?days=1")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["model_id"] for m in body["models"]] == ["opus", "sonnet"]
    assert "generated_at" in body


def test_top_models_endpoint_second_call_is_served_from_cache(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, model_id="opus")

    client = _client()
    first = client.get("/api/v1/usage/top-models?days=1")
    assert len(first.json()["models"]) == 1

    _event(session, "e2", now, model_id="sonnet")
    second = client.get("/api/v1/usage/top-models?days=1")
    assert len(second.json()["models"]) == 1, "expected the cached payload"

    cache_clear()
    third = client.get("/api/v1/usage/top-models?days=1")
    assert len(third.json()["models"]) == 2


# ---------------------------------------------------------------------------
# /top-projects
# ---------------------------------------------------------------------------


def test_top_projects_endpoint_returns_ranked_projects(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, project="runway", tokens_input=1000, tokens_output=0)
    _event(session, "e2", now, project="other-repo", tokens_input=10, tokens_output=0)

    resp = _client().get("/api/v1/usage/top-projects?days=1")
    assert resp.status_code == 200
    projects = [p["project"] for p in resp.json()["projects"]]
    assert projects == ["runway", "other-repo"]


def test_top_projects_endpoint_provider_id_filter_is_case_insensitive(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, provider_id="anthropic", project="runway")
    _event(session, "e2", now, provider_id="chatgpt", project="other-repo")

    resp = _client().get("/api/v1/usage/top-projects?days=1&provider_id=ANTHROPIC")
    projects = [p["project"] for p in resp.json()["projects"]]
    assert projects == ["runway"]


def test_top_projects_endpoint_second_call_is_served_from_cache(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, project="runway")

    client = _client()
    first = client.get("/api/v1/usage/top-projects?days=1")
    assert len(first.json()["projects"]) == 1

    _event(session, "e2", now, project="other-repo")
    second = client.get("/api/v1/usage/top-projects?days=1")
    assert len(second.json()["projects"]) == 1, "expected the cached payload"

    cache_clear()
    third = client.get("/api/v1/usage/top-projects?days=1")
    assert len(third.json()["projects"]) == 2


# ---------------------------------------------------------------------------
# /top-tools
# ---------------------------------------------------------------------------


def test_top_tools_endpoint_returns_ranked_tools(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, tools_json='["Read", "Edit"]')
    _event(session, "e2", now, tools_json='["Read"]')

    resp = _client().get("/api/v1/usage/top-tools?days=1")
    assert resp.status_code == 200
    tools = {t["tool"]: t["calls"] for t in resp.json()["tools"]}
    assert tools == {"Read": 2, "Edit": 1}


def test_top_tools_endpoint_provider_id_filter_is_case_insensitive(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, provider_id="anthropic", tools_json='["Read"]')
    _event(session, "e2", now, provider_id="chatgpt", tools_json='["Edit"]')

    resp = _client().get("/api/v1/usage/top-tools?days=1&provider_id=  Anthropic  ")
    tools = [t["tool"] for t in resp.json()["tools"]]
    assert tools == ["Read"]


def test_top_tools_endpoint_second_call_is_served_from_cache(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, tools_json='["Read"]')

    client = _client()
    first = client.get("/api/v1/usage/top-tools?days=1")
    assert len(first.json()["tools"]) == 1

    _event(session, "e2", now, tools_json='["Edit"]')
    second = client.get("/api/v1/usage/top-tools?days=1")
    assert len(second.json()["tools"]) == 1, "expected the cached payload"

    cache_clear()
    third = client.get("/api/v1/usage/top-tools?days=1")
    assert len(third.json()["tools"]) == 2


# ---------------------------------------------------------------------------
# /global-stats
# ---------------------------------------------------------------------------


def test_global_stats_endpoint_returns_lifetime_totals(session):
    _rollup(session, provider_id="anthropic", msgs=5)

    resp = _client().get("/api/v1/usage/global-stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["lifetime"]["msgs"] == 5
    assert body["distinct_providers"] == 1


def test_global_stats_endpoint_second_call_is_served_from_cache(session):
    _rollup(session, provider_id="anthropic", msgs=5)

    client = _client()
    first = client.get("/api/v1/usage/global-stats")
    assert first.json()["lifetime"]["msgs"] == 5

    session.exec(delete(UsagePeriodRollup))
    session.commit()

    second = client.get("/api/v1/usage/global-stats")
    assert second.json()["lifetime"]["msgs"] == 5, "expected the cached payload"

    cache_clear()
    third = client.get("/api/v1/usage/global-stats")
    assert third.json()["lifetime"]["msgs"] == 0


# ---------------------------------------------------------------------------
# reset/collect must invalidate the cache they'd otherwise sit behind
# ---------------------------------------------------------------------------


def test_reset_provider_invalidates_a_stale_top_models_cache(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, model_id="opus")

    client = _client()
    client.get("/api/v1/usage/top-models?days=1")  # warm the cache

    from unittest.mock import AsyncMock, patch

    with patch("app.api.endpoints.usage.manager.reset_collector", new_callable=AsyncMock):
        client.post("/api/v1/usage/reset/anthropic")

    _event(session, "e2", now, model_id="sonnet")
    resp = client.get("/api/v1/usage/top-models?days=1")
    assert len(resp.json()["models"]) == 2, "reset should have invalidated the warm cache entry"
