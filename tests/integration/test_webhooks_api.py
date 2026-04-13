from unittest.mock import AsyncMock, MagicMock, patch

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


def test_list_webhooks_empty(client):
    response = client.get("/api/v1/system/webhooks")
    assert response.status_code == 200
    assert response.json() == {"webhooks": []}


def test_create_webhook(client):
    payload = {
        "provider_id": "anthropic",
        "threshold_pct": 90.0,
        "url": "https://discord.example.com/hook",
        "channel": "discord",
    }
    response = client.post("/api/v1/system/webhooks", json=payload)
    assert response.status_code == 201
    assert "id" in response.json()


def test_list_webhooks_after_create(client):
    payload = {
        "provider_id": "openai",
        "threshold_pct": 85.0,
        "url": "https://hooks.slack.com/example",
        "channel": "slack",
    }
    client.post("/api/v1/system/webhooks", json=payload)
    response = client.get("/api/v1/system/webhooks")
    webhooks = response.json()["webhooks"]
    assert len(webhooks) == 1
    assert webhooks[0]["provider_id"] == "openai"
    assert webhooks[0]["threshold_pct"] == 85.0


def test_patch_webhook(client):
    create_resp = client.post(
        "/api/v1/system/webhooks",
        json={
            "provider_id": "anthropic",
            "threshold_pct": 90.0,
            "url": "https://discord.example.com/hook",
            "channel": "discord",
        },
    )
    webhook_id = create_resp.json()["id"]

    patch_resp = client.patch(f"/api/v1/system/webhooks/{webhook_id}", json={"threshold_pct": 75.0})
    assert patch_resp.status_code == 200

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"][0]["threshold_pct"] == 75.0


def test_delete_webhook(client):
    create_resp = client.post(
        "/api/v1/system/webhooks",
        json={
            "provider_id": "anthropic",
            "threshold_pct": 90.0,
            "url": "https://discord.example.com/hook",
            "channel": "discord",
        },
    )
    webhook_id = create_resp.json()["id"]

    del_resp = client.delete(f"/api/v1/system/webhooks/{webhook_id}")
    assert del_resp.status_code == 204

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"] == []


def test_patch_nonexistent_webhook(client):
    response = client.patch("/api/v1/system/webhooks/9999", json={"threshold_pct": 50.0})
    assert response.status_code == 404


def test_delete_nonexistent_webhook(client):
    response = client.delete("/api/v1/system/webhooks/9999")
    assert response.status_code == 404


def test_test_endpoint_sends_payload(client):
    """Test endpoint fires a webhook and returns status=sent."""
    create_resp = client.post(
        "/api/v1/system/webhooks",
        json={
            "provider_id": "anthropic",
            "threshold_pct": 90.0,
            "url": "https://discord.example.com/hook",
            "channel": "discord",
        },
    )
    webhook_id = create_resp.json()["id"]

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        response = client.post(f"/api/v1/system/webhooks/{webhook_id}/test")

    assert response.status_code == 200
    assert response.json() == {"status": "sent"}


def test_test_endpoint_returns_502_on_delivery_failure(client):
    """Test endpoint returns 502 when webhook delivery fails."""
    create_resp = client.post(
        "/api/v1/system/webhooks",
        json={
            "provider_id": "anthropic",
            "threshold_pct": 90.0,
            "url": "https://discord.example.com/hook",
            "channel": "discord",
        },
    )
    webhook_id = create_resp.json()["id"]

    import httpx as _httpx

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=_httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        )
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        response = client.post(f"/api/v1/system/webhooks/{webhook_id}/test")

    assert response.status_code == 502


def test_test_endpoint_404_on_nonexistent(client):
    """Test endpoint returns 404 for unknown webhook id."""
    response = client.post("/api/v1/system/webhooks/9999/test")
    assert response.status_code == 404
