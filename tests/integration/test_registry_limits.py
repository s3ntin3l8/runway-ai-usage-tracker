"""Integration tests for GET /api/v1/usage/limits endpoint with LatestUsage DB."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app
from app.models.db import LatestUsage


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def setup_api(session):
    app.dependency_overrides[get_session] = lambda: session
    yield
    app.dependency_overrides.clear()


def test_limits_serves_from_db(session: Session):
    card = {
        "service_name": "TestService",
        "provider_id": "test",
        "account_id": "1",
        "icon": "❓",
        "remaining": "100",
        "unit": "tokens",
        "health": "good",
    }
    record = LatestUsage(
        provider_id="test",
        account_id="1",
        sidecar_id="local",
        window_type="monthly",
        variant="default",
        card_json=json.dumps(card),
    )
    session.add(record)
    session.commit()

    client = TestClient(app)
    response = client.get("/api/v1/usage/limits")
    assert response.status_code == 200
    data = response.json()
    assert "limits" in data
    assert any(item["service_name"] == "TestService" for item in data["limits"])


def test_limits_fallback_when_db_empty(session: Session):
    client = TestClient(app)
    response = client.get("/api/v1/usage/limits")
    assert response.status_code == 200
    data = response.json()
    assert "limits" in data
    assert isinstance(data["limits"], list)
