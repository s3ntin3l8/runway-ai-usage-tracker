import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app
from app.models.db import UsageSnapshot


# Create a temporary database for testing
@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        yield session

    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_get_history_empty(client: TestClient):
    response = client.get("/api/v1/usage/history")
    assert response.status_code == 200
    assert response.json() == []


def test_get_history_with_data(client: TestClient, session: Session):
    # Add some dummy data
    now = datetime.now(UTC)
    s1 = UsageSnapshot(
        provider_id="anthropic",
        account_id="user1",
        service_name="Claude Pro",
        health="good",
        data_source="api",
        timestamp=now,
    )
    s2 = UsageSnapshot(
        provider_id="openai",
        account_id="user2",
        service_name="ChatGPT Plus",
        health="warning",
        data_source="api",
        timestamp=now - timedelta(hours=1),
    )
    session.add(s1)
    session.add(s2)
    session.commit()

    response = client.get("/api/v1/usage/history")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["provider_id"] == "anthropic"
    assert data[1]["provider_id"] == "openai"


def test_get_history_filtering(client: TestClient, session: Session):
    now = datetime.now(UTC)
    s1 = UsageSnapshot(
        provider_id="anthropic",
        account_id="user1",
        service_name="Claude Pro",
        health="good",
        data_source="api",
        timestamp=now,
    )
    s2 = UsageSnapshot(
        provider_id="openai",
        account_id="user2",
        service_name="ChatGPT Plus",
        health="warning",
        data_source="api",
        timestamp=now,
    )
    session.add(s1)
    session.add(s2)
    session.commit()

    response = client.get("/api/v1/usage/history?provider_id=anthropic")
    assert len(response.json()) == 1
    assert response.json()[0]["provider_id"] == "anthropic"


def test_get_history_limit(client: TestClient, session: Session):
    now = datetime.now(UTC)
    for i in range(10):
        s = UsageSnapshot(
            provider_id="test",
            account_id="user1",
            service_name=f"Service {i}",
            health="good",
            data_source="api",
            timestamp=now - timedelta(minutes=i),
        )
        session.add(s)
    session.commit()

    response = client.get("/api/v1/usage/history?limit=5")
    assert len(response.json()) == 5
