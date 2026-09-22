from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import ProviderConfig


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


# ---------------------------------------------------------------------------
# Per-account scoping (#274)
# ---------------------------------------------------------------------------


def _seed_account(session, provider_id="anthropic", account_id="work@example.com"):
    session.add(ProviderConfig(provider_id=provider_id, account_id=account_id))
    session.commit()


def _payload(**overrides):
    payload = {
        "provider_id": "anthropic",
        "threshold_pct": 90.0,
        "url": "https://discord.example.com/hook",
        "channel": "discord",
    }
    payload.update(overrides)
    return payload


def test_create_webhook_with_account_id(client, session):
    _seed_account(session)
    response = client.post(
        "/api/v1/system/webhooks",
        json=_payload(account_id="work@example.com"),
    )
    assert response.status_code == 201

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"][0]["account_id"] == "work@example.com"


def test_list_webhook_without_account_is_null(client):
    response = client.post("/api/v1/system/webhooks", json=_payload())
    assert response.status_code == 201

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"][0]["account_id"] is None


def test_create_webhook_unknown_account_400(client):
    response = client.post(
        "/api/v1/system/webhooks",
        json=_payload(account_id="nobody@example.com"),
    )
    assert response.status_code == 400
    assert "provider_configs" in response.json()["detail"]


def test_create_webhook_wildcard_provider_with_account_400(client):
    response = client.post(
        "/api/v1/system/webhooks",
        json=_payload(provider_id="*", account_id="work@example.com"),
    )
    assert response.status_code == 400
    assert "account_id" in response.json()["detail"]


def test_create_duplicate_all_accounts_409(client):
    """NULL account duplicates are rejected by the API (SQLite NULLs are distinct)."""
    assert client.post("/api/v1/system/webhooks", json=_payload()).status_code == 201
    dup = client.post("/api/v1/system/webhooks", json=_payload())
    assert dup.status_code == 409


def test_create_duplicate_scoped_account_409(client, session):
    _seed_account(session)
    assert (
        client.post(
            "/api/v1/system/webhooks", json=_payload(account_id="work@example.com")
        ).status_code
        == 201
    )
    dup = client.post("/api/v1/system/webhooks", json=_payload(account_id="work@example.com"))
    assert dup.status_code == 409


def test_patch_set_and_clear_account_id(client, session):
    _seed_account(session)
    create_resp = client.post("/api/v1/system/webhooks", json=_payload())
    webhook_id = create_resp.json()["id"]

    # Scope to an account.
    patch_resp = client.patch(
        f"/api/v1/system/webhooks/{webhook_id}",
        json={"account_id": "work@example.com"},
    )
    assert patch_resp.status_code == 200
    assert (
        client.get("/api/v1/system/webhooks").json()["webhooks"][0]["account_id"]
        == "work@example.com"
    )

    # Explicit null clears back to "All accounts".
    patch_resp = client.patch(
        f"/api/v1/system/webhooks/{webhook_id}",
        json={"account_id": None},
    )
    assert patch_resp.status_code == 200
    assert client.get("/api/v1/system/webhooks").json()["webhooks"][0]["account_id"] is None


def test_patch_account_omitted_leaves_scope_untouched(client, session):
    _seed_account(session)
    create_resp = client.post(
        "/api/v1/system/webhooks", json=_payload(account_id="work@example.com")
    )
    webhook_id = create_resp.json()["id"]

    patch_resp = client.patch(f"/api/v1/system/webhooks/{webhook_id}", json={"threshold_pct": 75.0})
    assert patch_resp.status_code == 200
    webhooks = client.get("/api/v1/system/webhooks").json()["webhooks"]
    assert webhooks[0]["account_id"] == "work@example.com"
    assert webhooks[0]["threshold_pct"] == 75.0


def test_patch_unknown_account_400(client):
    create_resp = client.post("/api/v1/system/webhooks", json=_payload())
    webhook_id = create_resp.json()["id"]

    patch_resp = client.patch(
        f"/api/v1/system/webhooks/{webhook_id}",
        json={"account_id": "nobody@example.com"},
    )
    assert patch_resp.status_code == 400


# ---------------------------------------------------------------------------
# Upgrade-path guard
# ---------------------------------------------------------------------------


def test_deferred_columns_includes_webhook_account_id():
    """account_id column must be in _DEFERRED_COLUMNS for existing DB upgrades."""
    from app.core.db import _DEFERRED_COLUMNS

    assert ("webhook_configs", "account_id", "VARCHAR") in _DEFERRED_COLUMNS
