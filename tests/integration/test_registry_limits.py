"""Tests for the registry-backed /limits endpoint (Phase 4C)."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from app.main import app
from app.services.collector_manager import manager


@pytest.fixture
def client():
    return TestClient(app)


def test_limits_serves_from_registry(client):
    """When registry has cards, /limits returns them without calling collect_all."""
    fixture_cards = [
        {
            "service_name": "Test Service",
            "icon": "🧪",
            "remaining": "50%",
            "unit": "capacity",
            "reset": "—",
            "health": "good",
            "pace": "Stable",
            "detail": "Test detail",
        }
    ]
    with patch.object(manager, "_registry", fixture_cards):
        with patch.object(manager, "collect_all", new_callable=AsyncMock) as mock_collect:
            response = client.get("/api/v1/usage/limits")
            assert response.status_code == 200
            data = response.json()
            assert len(data["limits"]) == 1
            assert data["limits"][0]["service_name"] == "Test Service"
            # collect_all should NOT have been called — registry was populated
            mock_collect.assert_not_called()


def test_limits_fallback_when_registry_empty(client):
    """When registry is empty, /limits falls back to collect_all."""
    fresh_cards = [
        {
            "service_name": "Fresh Service",
            "icon": "✨",
            "remaining": "80%",
            "unit": "capacity",
            "reset": "—",
            "health": "good",
            "pace": "Stable",
            "detail": "Fresh detail",
        }
    ]
    with patch.object(manager, "_registry", []):
        with patch.object(
            manager, "collect_all", new_callable=AsyncMock, return_value=fresh_cards
        ) as mock_collect:
            response = client.get("/api/v1/usage/limits")
            assert response.status_code == 200
            data = response.json()
            assert len(data["limits"]) == 1
            assert data["limits"][0]["service_name"] == "Fresh Service"
            # collect_all should have been called as fallback
            mock_collect.assert_called_once()


def test_get_registry_snapshot_returns_copy():
    """get_registry_snapshot returns a copy, not a reference."""
    cards = [{"service_name": "Test", "icon": "T", "remaining": "50%",
              "unit": "u", "reset": "—", "health": "good", "pace": "Stable", "detail": "d"}]
    with patch.object(manager, "_registry", cards):
        snapshot = manager.get_registry_snapshot()
        assert snapshot == cards
        # Mutating the snapshot should not affect the registry
        snapshot.append({"service_name": "Extra"})
        assert len(manager._registry) == 1
