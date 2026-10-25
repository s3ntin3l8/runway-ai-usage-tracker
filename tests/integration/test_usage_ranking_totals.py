"""The rankings report how many groups they left out (#949).

The five top-N endpoints cap their rows with a SQL ``LIMIT``. The frontend
wants to say "top 12 of 34", so every ranking response carries ``total`` — the
number of ranked groups before the ``LIMIT`` truncated the result — next to the
``limit`` that was applied.

The queries themselves are covered in ``tests/services/test_query_top_*.py``;
this covers the wiring the query alone can't — the response model and the
cache, which has to serve the rows and the count together.
"""

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.cache import cache_clear
from app.core.db import get_session
from app.main import app
from app.models.db import UsageEvent
from tests.fixtures.test_client import TestClient


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
    model_id: str | None = "opus",
    tools_json: str | None = None,
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
            project=project,
            tools_json=tools_json,
            session_id=session_id,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=cost_usd,
        )
    )
    session.commit()


def _client() -> TestClient:
    return TestClient(app)


def test_top_models_reports_the_total_behind_the_cap(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, model_id="opus", tokens_input=1000)
    _event(session, "e2", now, model_id="sonnet", tokens_input=100)
    _event(session, "e3", now, model_id="flash", tokens_input=10)

    body = _client().get("/api/v1/usage/top-models?days=1&limit=2").json()

    assert len(body["models"]) == 2
    assert body["total"] == 3
    assert body["limit"] == 2


def test_top_models_cached_response_carries_the_same_numbers(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, model_id="opus", tokens_input=1000)
    _event(session, "e2", now, model_id="sonnet", tokens_input=100)

    client = _client()
    first = client.get("/api/v1/usage/top-models?days=1&limit=1").json()
    second = client.get("/api/v1/usage/top-models?days=1&limit=1").json()

    assert first["total"] == second["total"] == 2
    assert first["limit"] == second["limit"] == 1


def test_top_models_total_follows_the_sidecar_filter(session):
    """The count is over the same filter set as the rows."""
    now = datetime.now(UTC)
    _event(session, "e1", now, model_id="opus", sidecar_id="host-a")
    _event(session, "e2", now, model_id="sonnet", sidecar_id="host-b")

    body = _client().get("/api/v1/usage/top-models?days=1&sidecar_id=host-b").json()

    assert [m["model_id"] for m in body["models"]] == ["sonnet"]
    assert body["total"] == 1


def test_top_hosts_reports_the_total_behind_the_cap(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, sidecar_id="host-a", tokens_input=1000)
    _event(session, "e2", now, sidecar_id="host-b", tokens_input=100)
    _event(session, "e3", now, sidecar_id="host-c", tokens_input=10)

    body = _client().get("/api/v1/usage/top-hosts?days=1&limit=2").json()

    assert len(body["hosts"]) == 2
    assert body["total"] == 3
    assert body["limit"] == 2


def test_top_tools_reports_the_total_behind_the_cap(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, tools_json='["Read", "Write", "Bash"]')

    body = _client().get("/api/v1/usage/top-tools?days=1&limit=1").json()

    assert len(body["tools"]) == 1
    assert body["total"] == 3
    assert body["limit"] == 1


def test_top_tools_total_ignores_an_unnamed_tool(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, tools_json='["Read", ""]')

    body = _client().get("/api/v1/usage/top-tools?days=1").json()

    assert [t["tool"] for t in body["tools"]] == ["Read"]
    assert body["total"] == 1


def test_top_projects_reports_the_total_behind_the_cap(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, project="alpha", tokens_input=1000)
    _event(session, "e2", now, project="beta", tokens_input=100)
    _event(session, "e3", now, project="gamma", tokens_input=10)

    body = _client().get("/api/v1/usage/top-projects?days=1&limit=2").json()

    assert len(body["projects"]) == 2
    assert body["total"] == 3
    assert body["limit"] == 2


def test_top_sessions_reports_the_total_behind_the_cap(session):
    now = datetime.now(UTC)
    _event(session, "e1", now, session_id="s1", tokens_input=1000)
    _event(session, "e2", now, session_id="s2", tokens_input=100)
    _event(session, "e3", now, session_id="s3", tokens_input=10)

    body = _client().get("/api/v1/usage/top-sessions?days=1&limit=2").json()

    assert len(body["sessions"]) == 2
    assert body["total"] == 3
    assert body["limit"] == 2
