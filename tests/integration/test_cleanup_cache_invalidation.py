"""POST /api/v1/system/cleanup and /api/v1/system/force-collect must not
leave the response cache serving stale/deleted rows for up to the cache's
TTL after they mutate LatestUsage."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import app.core.db as db_module
from app.core.cache import cache_get, cache_set
from app.core.db import get_session
from app.main import app


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session):
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_cleanup_with_clear_cache_invalidates_response_cache(client):
    cache_set("fleet", {"stale": True}, ttl_seconds=60.0)

    with (
        patch("app.services.poller.poller.wake"),
        patch("app.services.poller.poller.poll_now", new_callable=AsyncMock),
    ):
        response = client.post("/api/v1/system/cleanup", json={"clear_cache": True})

    assert response.status_code == 200
    assert cache_get("fleet") is None


def test_cleanup_removing_inactive_sidecars_invalidates_response_cache(client):
    """Even with clear_cache=False, removing sidecars changes what /fleet
    reports — the response cache must still be dropped."""
    cache_set("fleet", {"stale": True}, ttl_seconds=60.0)

    response = client.post(
        "/api/v1/system/cleanup",
        json={"clear_cache": False, "remove_inactive_sidecars_days": 90},
    )

    assert response.status_code == 200
    assert cache_get("fleet") is None


def test_cleanup_with_no_mutations_leaves_response_cache_intact(client):
    """A no-op cleanup call (nothing to clear/prune/remove) shouldn't pay the
    cost of dropping an otherwise-still-valid cache."""
    cache_set("fleet", {"still_fresh": True}, ttl_seconds=60.0)

    response = client.post("/api/v1/system/cleanup", json={"clear_cache": False})

    assert response.status_code == 200
    assert cache_get("fleet") == {"still_fresh": True}


def test_force_collect_clears_response_cache(monkeypatch):
    """force_collect opens its own Session(app.core.db.engine) rather than
    going through the Depends(get_session) override, so this test swaps the
    module-level engine directly instead of using the `client` fixture."""
    test_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(db_module, "engine", test_engine)

    cache_set("fleet", {"stale": True}, ttl_seconds=60.0)

    with (
        patch("app.services.poller.poller.wake"),
        patch("app.services.poller.poller.poll_now", new_callable=AsyncMock),
    ):
        response = TestClient(app).post("/api/v1/system/force-collect")

    assert response.status_code == 200
    assert cache_get("fleet") is None
