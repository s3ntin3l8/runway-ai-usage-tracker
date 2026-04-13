import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, SQLModel
from sqlmodel.pool import StaticPool
from app.main import app
from app.core.db import get_session
from app.models.db import WebhookConfig


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
    create_resp = client.post("/api/v1/system/webhooks", json={
        "provider_id": "anthropic", "threshold_pct": 90.0,
        "url": "https://discord.example.com/hook", "channel": "discord",
    })
    webhook_id = create_resp.json()["id"]

    patch_resp = client.patch(f"/api/v1/system/webhooks/{webhook_id}",
                              json={"threshold_pct": 75.0})
    assert patch_resp.status_code == 200

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"][0]["threshold_pct"] == 75.0


def test_delete_webhook(client):
    create_resp = client.post("/api/v1/system/webhooks", json={
        "provider_id": "anthropic", "threshold_pct": 90.0,
        "url": "https://discord.example.com/hook", "channel": "discord",
    })
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
