import pytest
from fastapi.testclient import TestClient
from app.main import app
from unittest.mock import patch, AsyncMock

@pytest.fixture
def client():
    return TestClient(app)

def test_get_status(client):
    with patch("app.api.endpoints.status.manager.get_collector_stats") as mock_stats:
        mock_stats.return_value = {"collectors": [{"collector": "test", "status": "ok"}]}
        response = client.get("/api/status/")
        assert response.status_code == 200
        assert response.json()["collectors"][0]["collector"] == "test"

@pytest.mark.asyncio
async def test_reset_provider(client):
    with patch("app.api.routes.manager.reset_collector", new_callable=AsyncMock) as mock_reset:
        response = client.post("/api/reset/anthropic")
        assert response.status_code == 200
        assert response.json()["status"] == "reset"
        mock_reset.assert_called_once_with("anthropic", None)

@pytest.mark.asyncio
async def test_reset_provider_with_account(client):
    with patch("app.api.routes.manager.reset_collector", new_callable=AsyncMock) as mock_reset:
        response = client.post("/api/reset/anthropic?account_id=user123")
        assert response.status_code == 200
        mock_reset.assert_called_once_with("anthropic", "user123")
