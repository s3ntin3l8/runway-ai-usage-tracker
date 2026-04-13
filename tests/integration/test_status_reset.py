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
