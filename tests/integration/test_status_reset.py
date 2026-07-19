from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_get_status(client):
    with patch("app.api.endpoints.system.manager.get_collector_stats") as mock_stats:
        mock_stats.return_value = {"collectors": [{"collector": "test", "status": "ok"}]}
        response = client.get("/api/v1/system/status")
        assert response.status_code == 200
        assert response.json()["collectors"][0]["collector"] == "test"


@pytest.mark.asyncio
async def test_reset_provider(client):
    with patch(
        "app.api.endpoints.usage.manager.reset_collector", new_callable=AsyncMock
    ) as mock_reset:
        response = client.post("/api/v1/usage/reset/anthropic")
        assert response.status_code == 200
        assert response.json()["status"] == "reset"
        mock_reset.assert_called_once_with("anthropic", None)


@pytest.mark.asyncio
async def test_reset_provider_with_account(client):
    with patch(
        "app.api.endpoints.usage.manager.reset_collector", new_callable=AsyncMock
    ) as mock_reset:
        response = client.post("/api/v1/usage/reset/anthropic?account_id=user123")
        assert response.status_code == 200
        mock_reset.assert_called_once_with("anthropic", "user123")


@pytest.mark.asyncio
async def test_reset_provider_clears_response_cache(client):
    """A reset can flip a card's status — a stale cached /fleet or /forecast
    response must not keep serving the pre-reset state for up to the TTL."""
    from app.core.cache import cache_get, cache_set

    cache_set("fleet", {"stale": True}, ttl_seconds=60.0)
    with patch("app.api.endpoints.usage.manager.reset_collector", new_callable=AsyncMock):
        response = client.post("/api/v1/usage/reset/anthropic")
        assert response.status_code == 200
    assert cache_get("fleet") is None


@pytest.mark.asyncio
async def test_collect_provider_clears_response_cache(client):
    """A manual 'collect now' must be reflected immediately, not after the
    fleet/forecast/top-* cache TTL elapses."""
    from app.core.cache import cache_get, cache_set

    cache_set("fleet", {"stale": True}, ttl_seconds=60.0)
    with patch(
        "app.api.endpoints.usage.manager.collect_one", new_callable=AsyncMock
    ) as mock_collect:
        mock_collect.return_value = []
        response = client.post("/api/v1/usage/collect/anthropic")
        assert response.status_code == 200
    assert cache_get("fleet") is None
