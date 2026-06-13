"""Integration tests for /api/v1/system/debug/raw/{provider_id}.

Sidecar-only providers (e.g. antigravity) have no server-side collector, so a
raw capture must return an honest 404 — not a 500 that masks the real cause.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app


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


def test_debug_raw_unknown_provider_returns_404(client):
    # antigravity is sidecar-only — no server collector is registered for it.
    r = client.get("/api/v1/system/debug/raw/antigravity")
    assert r.status_code == 404
    assert "antigravity" in r.json()["detail"]
