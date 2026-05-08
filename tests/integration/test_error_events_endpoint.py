"""Integration tests for kind=error filtering on GET /api/v1/usage/events (Task 14.1)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import UsageEvent
from app.services.pricing_seed import seed_pricing_table


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        seed_pricing_table(s)
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


def _client():
    return TestClient(app)


def _event(event_id: str, kind: str = "message", **kw) -> UsageEvent:
    defaults = {
        "provider_id": "anthropic",
        "account_id": "user@example.com",
        "sidecar_id": "dev-01",
        "ts": datetime.now(UTC),
        "model_id": "sonnet",
        "tokens_input": 100,
        "tokens_output": 50,
        "cost_usd": 0.001,
    }
    defaults.update(kw)
    return UsageEvent(event_id=event_id, kind=kind, **defaults)


class TestEventsKindFilter:
    """Tests for the kind= query parameter on GET /api/v1/usage/events."""

    def test_events_endpoint_filters_by_kind_error(self, session):
        """Seed 2 normal + 1 error; kind=error → 1 result."""
        session.add(_event("msg_001", kind="message"))
        session.add(_event("msg_002", kind="message"))
        session.add(_event("err_001", kind="error", stop_reason="rate_limit"))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "kind": "error",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total"] == 1
        assert data["events"][0]["event_id"] == "err_001"
        assert data["events"][0]["kind"] == "error"

    def test_events_endpoint_filters_by_kind_message(self, session):
        """Seed 2 normal + 1 error; kind=message → 2 results."""
        session.add(_event("msg_001", kind="message"))
        session.add(_event("msg_002", kind="message"))
        session.add(_event("err_001", kind="error", stop_reason="timeout"))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "kind": "message",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total"] == 2
        assert all(e["kind"] == "message" for e in data["events"])

    def test_events_endpoint_no_kind_filter_returns_all(self, session):
        """Without a kind= filter all events (message + error) are returned."""
        session.add(_event("msg_001", kind="message"))
        session.add(_event("err_001", kind="error"))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
            },
        )
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_events_endpoint_kind_error_empty_when_no_errors(self, session):
        """kind=error returns empty list when only normal events exist."""
        session.add(_event("msg_001", kind="message"))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "kind": "error",
            },
        )
        assert r.status_code == 200
        assert r.json()["events"] == []
        assert r.json()["total"] == 0
