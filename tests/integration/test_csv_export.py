from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import UsageSnapshot


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session):
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _add_snapshot(session, provider="anthropic", used=100.0):
    snap = UsageSnapshot(
        timestamp=datetime.now(UTC),
        provider_id=provider,
        account_id="acc1",
        service_name="Test",
        used_value=used,
        limit_value=1000.0,
        unit_type="tokens",
        health="good",
        data_source="oauth",
        window_type="monthly",
    )
    session.add(snap)
    session.commit()
    return snap


def test_csv_export_returns_csv_content_type(client, session):
    _add_snapshot(session)
    response = client.get("/api/v1/usage/history?format=csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]


def test_csv_export_has_correct_headers(client, session):
    _add_snapshot(session)
    response = client.get("/api/v1/usage/history?format=csv")
    lines = response.text.strip().splitlines()
    header = lines[0]
    assert "timestamp" in header
    assert "provider_id" in header
    assert "used_value" in header
    assert "limit_value" in header
    assert "service_name" in header


def test_csv_export_contains_data_row(client, session):
    _add_snapshot(session, provider="openai", used=250.0)
    response = client.get("/api/v1/usage/history?format=csv")
    assert "openai" in response.text
    assert "250.0" in response.text


def test_csv_export_content_disposition(client, session):
    _add_snapshot(session)
    response = client.get("/api/v1/usage/history?format=csv")
    disposition = response.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "runway-history-" in disposition
    assert ".csv" in disposition


def test_json_format_still_works(client, session):
    """Default (JSON) format is unaffected."""
    _add_snapshot(session)
    response = client.get("/api/v1/usage/history")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
