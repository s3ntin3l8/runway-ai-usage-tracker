"""Integration tests for the sidecar update channel in /system/app-config."""

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


def test_default_channel_is_stable(client: TestClient):
    r = client.get("/api/v1/system/app-config")
    assert r.status_code == 200
    assert r.json()["sidecar_update_channel"] == "stable"


def test_set_edge_channel_roundtrip(client: TestClient):
    r = client.put("/api/v1/system/app-config", json={"sidecar_update_channel": "edge"})
    assert r.status_code == 200
    assert client.get("/api/v1/system/app-config").json()["sidecar_update_channel"] == "edge"


def test_clear_channel_returns_to_stable(client: TestClient):
    client.put("/api/v1/system/app-config", json={"sidecar_update_channel": "edge"})
    client.put("/api/v1/system/app-config", json={"sidecar_update_channel": "stable"})
    assert client.get("/api/v1/system/app-config").json()["sidecar_update_channel"] == "stable"


def test_invalid_channel_rejected(client: TestClient):
    r = client.put("/api/v1/system/app-config", json={"sidecar_update_channel": "nightly"})
    assert r.status_code == 400


def test_default_auto_update_is_off(client: TestClient):
    r = client.get("/api/v1/system/app-config")
    assert r.status_code == 200
    assert r.json()["sidecar_auto_update"] is False


def test_set_auto_update_roundtrip(client: TestClient):
    r = client.put("/api/v1/system/app-config", json={"sidecar_auto_update": True})
    assert r.status_code == 200
    assert client.get("/api/v1/system/app-config").json()["sidecar_auto_update"] is True
    client.put("/api/v1/system/app-config", json={"sidecar_auto_update": False})
    assert client.get("/api/v1/system/app-config").json()["sidecar_auto_update"] is False


def test_auto_update_unset_preserves_value(client: TestClient):
    # Omitting the field in a PUT must not clobber the stored value.
    client.put("/api/v1/system/app-config", json={"sidecar_auto_update": True})
    client.put("/api/v1/system/app-config", json={"sidecar_update_channel": "edge"})
    assert client.get("/api/v1/system/app-config").json()["sidecar_auto_update"] is True
