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
    # Space across distinct hours so hourly bucketing doesn't collapse them.
    for i in range(10):
        s = UsageSnapshot(
            provider_id="test",
            account_id="user1",
            service_name=f"Service {i}",
            health="good",
            data_source="api",
            timestamp=now - timedelta(hours=i),
        )
        session.add(s)
    session.commit()

    response = client.get("/api/v1/usage/history?limit=5&days=1")
    assert len(response.json()) == 5


def test_get_history_multi_day_not_truncated_by_limit(
    client: TestClient, session: Session
):
    """Regression: high-volume today must not push older days out of the response.

    With a flat `ORDER BY timestamp DESC LIMIT N`, a day with >N snapshots consumes
    the whole budget and the caller never sees older days. Server-side hourly
    bucketing per (provider, account, model, window, unit) must preserve coverage
    across the full time window.
    """
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    # 3 days × 600 rows/day, all same group key within each hour — real pollers
    # produce many rows per hour for the same card (different values, same key).
    for day in range(3):
        for i in range(600):
            s = UsageSnapshot(
                provider_id="anthropic",
                account_id="user1",
                model_id="claude-sonnet",
                window_type="5hr_limit",
                unit_type="percent",
                used_value=float(i % 100),
                service_name="Claude",
                health="good",
                data_source="api",
                timestamp=now - timedelta(days=day, minutes=i),
            )
            session.add(s)
    session.commit()

    response = client.get("/api/v1/usage/history?days=7&limit=500")
    assert response.status_code == 200
    data = response.json()

    # Each day should contribute roughly ~10 hourly buckets (600 rows spread over
    # ~10 hours). All 3 days must appear — this is the bug being regressed.
    days_present = {row["timestamp"][:10] for row in data}
    assert len(days_present) == 3, (
        f"Expected rows from 3 distinct days, got {len(days_present)}: {days_present}"
    )
