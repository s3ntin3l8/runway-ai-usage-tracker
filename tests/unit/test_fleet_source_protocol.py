"""Source protocol integration uses synthetic databases and HMAC keys only."""

import hashlib
import hmac
import json
import time

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.endpoints.fleet_sources import router
from app.core.config import settings
from app.core.db import get_session
from app.core.encryption import EncryptionService
from app.models.db import SidecarDevice
from app.services import sidecar_devices, source_controls
from scripts.sidecar_pkg.source_catalog import catalog


@pytest.fixture
def protocol(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    encryption = EncryptionService(Fernet.generate_key().decode())
    monkeypatch.setattr(sidecar_devices, "encryption_service", encryption)
    monkeypatch.setattr(source_controls, "encryption_service", encryption)
    monkeypatch.setattr(settings, "INGEST_API_KEY", "synthetic-shared")
    monkeypatch.setattr(settings, "APP_HOST", "127.0.0.1")
    with Session(engine) as session:
        device, secret = sidecar_devices.enroll(session, "synthetic")
        session.commit()
        app = FastAPI()
        app.include_router(router, prefix="/fleet")
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client, session, device.device_id, secret
    engine.dispose()


def report(read=True, generation=1):
    descriptor = catalog()["anthropic.claude-code"]
    return {
        "source_id": descriptor.id,
        "provider_id": descriptor.provider,
        "label": descriptor.label,
        "revision": descriptor.revision,
        "generation": generation,
        "read": read,
        "state": "Limited",
        "new": True,
    }


def sync(client, device_id, secret, payload, *, authenticated=True):
    body = json.dumps(payload).encode()
    timestamp = str(time.time())
    message = (
        sidecar_devices.message(device_id, timestamp, "source-sync", body)
        if authenticated
        else timestamp.encode() + body
    )
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    headers = {"X-Signature": signature, "X-Timestamp": timestamp}
    if authenticated:
        headers["X-Runway-Device-Id"] = device_id
    return client.post("/fleet/source-sync", content=body, headers=headers)


def test_shared_key_cannot_control_enrolled_sources(protocol):
    client, session, device_id, secret = protocol
    assert (
        sync(
            client, device_id, "synthetic-shared", {"sources": [report()]}, authenticated=False
        ).status_code
        == 403
    )
    assert sync(client, device_id, "synthetic-shared", {"sources": [report()]}).status_code == 401
    assert source_controls.state(session, device_id, "anthropic.claude-code") is None


def test_sync_commands_and_offline_withdrawal_are_ordered_and_idempotent(protocol):
    client, session, device_id, secret = protocol
    response = sync(client, device_id, secret, {"sources": [report()]})
    assert response.status_code == 200
    assert response.json()["accepted"] == ["anthropic.claude-code"]
    base = "/fleet/sidecars/" + sidecar_devices.registry_id(device_id) + "/sources"
    paused = client.post(base + "/anthropic.claude-code/commands", json={"action": "pause"})
    assert paused.status_code == 200
    command = paused.json()["id"]
    response = sync(
        client,
        device_id,
        secret,
        {"sources": [report(False, 2)], "acknowledgments": [{"id": command, "status": "applied"}]},
    )
    assert response.json()["withdrawals"] == [
        {"source_id": "anthropic.claude-code", "generation": 2}
    ]
    assert response.json()["commands"] == []
    assert (
        client.post(base + "/anthropic.claude-code/commands", json={"action": "resume"}).status_code
        == 409
    )
    status = client.get(base).json()["sources"][0]
    assert status["read"] is False and status["remote_paused"] is True
    assert status["report_stale"] is False


@pytest.mark.parametrize(
    "bad",
    [
        {"read": "true"},
        {"generation": True},
        {"revision": "unknown"},
        {"unexpected": "synthetic-secret"},
    ],
)
def test_invalid_report_does_not_change_consent_or_echo_payload(protocol, bad):
    client, session, device_id, secret = protocol
    response = sync(client, device_id, secret, {"sources": [{**report(), **bad}]})
    assert response.status_code == 422
    assert "synthetic-secret" not in response.text
    assert source_controls.state(session, device_id, "anthropic.claude-code") is None


def test_status_loads_credential_health_once_for_multiple_sources(protocol):
    from sqlalchemy import event

    client, session, device_id, secret = protocol
    second = catalog()["github.gh-cli"]
    reports = [
        report(),
        {
            **report(),
            "source_id": second.id,
            "provider_id": second.provider,
            "revision": second.revision,
        },
    ]
    assert sync(client, device_id, secret, {"sources": reports}).status_code == 200
    statements = []

    def capture(conn, cursor, statement, params, context, many):
        statements.append(statement.lower())

    event.listen(session.get_bind(), "before_cursor_execute", capture)
    try:
        response = client.get(
            "/fleet/sidecars/" + sidecar_devices.registry_id(device_id) + "/sources"
        )
        assert response.status_code == 200
        assert len(response.json()["sources"]) == 2
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", capture)
    assert sum("from credential_sources" in sql for sql in statements) == 1
    assert sum("from sidecar_source_contributions" in sql for sql in statements) == 1


def test_only_an_accepted_sync_stamps_the_device(protocol):
    client, session, device_id, secret = protocol
    # A rejected request (wrong key) must not count as a completed sync.
    assert sync(client, device_id, "synthetic-shared", {"sources": [report()]}).status_code == 401
    session.expire_all()
    assert session.get(SidecarDevice, device_id).last_source_sync_at is None
    assert sync(client, device_id, secret, {"sources": [report()]}).status_code == 200
    session.expire_all()
    assert session.get(SidecarDevice, device_id).last_source_sync_at is not None
