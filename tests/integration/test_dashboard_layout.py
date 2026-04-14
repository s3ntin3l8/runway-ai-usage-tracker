import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app


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


def test_get_dashboard_layout_empty_default(client: TestClient):
    r = client.get("/api/v1/system/dashboard-layout")
    assert r.status_code == 200
    assert r.json() == {"provider_order": [], "card_orders": {}}


def test_put_and_get_roundtrip(client: TestClient):
    body = {
        "provider_order": ["anthropic", "gemini"],
        "card_orders": {"anthropic": ["acc1|Claude Pro||5hr_limit"]},
    }
    r = client.put("/api/v1/system/dashboard-layout", json=body)
    assert r.status_code == 200
    assert r.json() == {"status": "saved"}

    r2 = client.get("/api/v1/system/dashboard-layout")
    assert r2.status_code == 200
    assert r2.json() == body


def test_put_malformed_rejected(client: TestClient):
    # provider_order must be a list of strings
    r = client.put(
        "/api/v1/system/dashboard-layout",
        json={"provider_order": [1, 2], "card_orders": {}},
    )
    assert r.status_code == 422


def test_put_overwrites_previous(client: TestClient):
    client.put(
        "/api/v1/system/dashboard-layout",
        json={"provider_order": ["a"], "card_orders": {}},
    )
    client.put(
        "/api/v1/system/dashboard-layout",
        json={"provider_order": ["b"], "card_orders": {}},
    )
    r = client.get("/api/v1/system/dashboard-layout")
    assert r.json()["provider_order"] == ["b"]
