"""Integration tests for fleet sidecar CRUD endpoints (Phase 4B)."""

from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import SidecarRegistry


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
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_list_sidecars_empty(client):
    response = client.get("/api/v1/fleet/sidecars")
    assert response.status_code == 200
    assert response.json() == {"sidecars": []}


def test_list_sidecars_returns_registered(client, session):
    from datetime import datetime

    row = SidecarRegistry(
        sidecar_id="test-host",
        hostname="test-host",
        last_ip="10.0.0.1",
        last_seen=datetime(2026, 4, 13, tzinfo=UTC),
        first_seen=datetime(2026, 4, 1, tzinfo=UTC),
    )
    session.add(row)
    session.commit()

    response = client.get("/api/v1/fleet/sidecars")
    assert response.status_code == 200
    sidecars = response.json()["sidecars"]
    assert len(sidecars) == 1
    assert sidecars[0]["sidecar_id"] == "test-host"


def test_get_sidecar(client, session):
    from datetime import datetime

    row = SidecarRegistry(
        sidecar_id="my-host",
        hostname="my-host",
        last_seen=datetime.now(UTC),
        first_seen=datetime.now(UTC),
    )
    session.add(row)
    session.commit()

    response = client.get("/api/v1/fleet/sidecars/my-host")
    assert response.status_code == 200
    assert response.json()["sidecar_id"] == "my-host"


def test_get_sidecar_not_found(client):
    response = client.get("/api/v1/fleet/sidecars/nonexistent")
    assert response.status_code == 404


def test_patch_sidecar_name_and_tags(client, session):
    from datetime import datetime

    row = SidecarRegistry(
        sidecar_id="patch-host",
        hostname="patch-host",
        last_seen=datetime.now(UTC),
        first_seen=datetime.now(UTC),
    )
    session.add(row)
    session.commit()

    response = client.patch(
        "/api/v1/fleet/sidecars/patch-host",
        json={"custom_name": "My Workstation", "tags": ["Work", "Primary"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["custom_name"] == "My Workstation"
    assert data["tags"] == ["Work", "Primary"]


def test_patch_sidecar_not_found(client):
    response = client.patch("/api/v1/fleet/sidecars/ghost", json={"custom_name": "Ghost"})
    assert response.status_code == 404


def test_delete_sidecar(client, session):
    from datetime import datetime

    row = SidecarRegistry(
        sidecar_id="del-host",
        hostname="del-host",
        last_seen=datetime.now(UTC),
        first_seen=datetime.now(UTC),
    )
    session.add(row)
    session.commit()

    response = client.delete("/api/v1/fleet/sidecars/del-host")
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"

    # Confirm it's gone
    response = client.get("/api/v1/fleet/sidecars/del-host")
    assert response.status_code == 404


def test_delete_sidecar_not_found(client):
    response = client.delete("/api/v1/fleet/sidecars/nonexistent")
    assert response.status_code == 404


def test_update_now_sets_pending_flag(client, session):
    from datetime import datetime

    from app.services.fleet_registry import fleet_registry

    row = SidecarRegistry(
        sidecar_id="upd-host",
        hostname="upd-host",
        last_seen=datetime.now(UTC),
        first_seen=datetime.now(UTC),
    )
    session.add(row)
    session.commit()

    response = client.post("/api/v1/fleet/sidecars/upd-host/update")
    assert response.status_code == 200
    assert response.json() == {"status": "update_queued", "sidecar_id": "upd-host"}

    # Persisted on the row (survives a server restart), not just in-memory —
    # regression for the dead-ingest incident where a queued update sat
    # unreachable for days; a restart during that window must not lose it.
    session.refresh(row)
    assert row.pending_update is True

    # The flag is delivered exactly once (consumed on the next heartbeat).
    assert fleet_registry.consume_pending_update("upd-host", session) is True
    assert fleet_registry.consume_pending_update("upd-host", session) is False


def test_update_now_unregistered_sidecar_404s(client):
    # No SidecarRegistry row exists yet — persisting the flag needs somewhere
    # to persist it, so this is a 404 (matches pause/resume for an unknown id)
    # rather than silently queuing for an id that may never check in.
    response = client.post("/api/v1/fleet/sidecars/never-seen/update")
    assert response.status_code == 404
