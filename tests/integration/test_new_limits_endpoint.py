# tests/integration/test_new_limits_endpoint.py
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.main import app
from app.models.db import LatestUsage


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


def test_fetch_limits_from_db(session: Session):
    # Setup test data in DB
    card_data = {
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
        card_json=json.dumps(card_data),
    )
    session.add(record)
    session.commit()

    # The get_session dependency needs to be overridden to use our test session
    from app.core.db import get_session

    app.dependency_overrides[get_session] = lambda: session

    client = TestClient(app)
    response = client.get("/api/v1/usage/limits")

    # Clean up override
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert "limits" in data

    # Find our test service in the response
    test_service = next(
        (item for item in data["limits"] if item.get("service_name") == "TestService"), None
    )
    assert test_service is not None
    assert test_service["service_name"] == "TestService"
