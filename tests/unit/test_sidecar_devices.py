"""Per-device credentials are independent of hostnames and legacy ingest keys."""

import hashlib
import hmac
import time

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from starlette.requests import Request

from app.core.encryption import EncryptionService
from app.services import sidecar_devices


@pytest.fixture
def session(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "INGEST_API_KEY", "legacy-shared-key")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(
        sidecar_devices, "encryption_service", EncryptionService(Fernet.generate_key().decode())
    )
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_enrollment_is_encrypted_and_hostname_is_only_a_label(session):
    first, secret = sidecar_devices.enroll(session, "same-host")
    second, second_secret = sidecar_devices.enroll(session, "same-host")
    assert first.device_id != second.device_id
    assert secret != second_secret
    assert first.secret_encrypted != secret
    assert sidecar_devices.authentication_key(session, first.device_id) == secret
    assert sidecar_devices.registry_id(first.device_id).startswith("device-")


def test_no_plaintext_device_enrollment(monkeypatch, session):
    monkeypatch.setattr(sidecar_devices, "encryption_service", EncryptionService())
    with pytest.raises(ValueError, match="encryption"):
        sidecar_devices.enroll(session, "synthetic")


def test_revoked_or_unknown_device_has_no_key(session):
    device, _ = sidecar_devices.enroll(session, "synthetic")
    device.revoked = True
    session.add(device)
    session.flush()
    assert sidecar_devices.authentication_key(session, device.device_id) is None
    assert sidecar_devices.authentication_key(session, "invalid") is None


@pytest.mark.parametrize("forged", [False, True])
async def test_auth_binds_device_and_operation(session, forged):
    from app.core.security import validate_ingest_auth

    device, secret = sidecar_devices.enroll(session, "synthetic")
    timestamp, body = str(time.time()), b"{}"
    key = "legacy-shared-key" if forged else secret
    message = sidecar_devices.message(device.device_id, timestamp, "source-sync", body)
    signature = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/fleet/source-sync",
            "headers": [(b"x-runway-device-id", device.device_id.encode())],
        },
        receive,
    )
    if forged:
        with pytest.raises(HTTPException) as result:
            await validate_ingest_auth(
                request, signature, timestamp, session=session, purpose="source-sync"
            )
        assert result.value.status_code == 401
    else:
        assert (
            await validate_ingest_auth(
                request, signature, timestamp, session=session, purpose="source-sync"
            )
            == body
        )
        assert request.state.sidecar_device_id == device.device_id


async def test_device_signature_cannot_cross_endpoints(session):
    from app.core.security import validate_ingest_auth

    device, secret = sidecar_devices.enroll(session, "synthetic")
    timestamp, body = str(time.time()), b"{}"
    signature = hmac.new(
        secret.encode(),
        sidecar_devices.message(device.device_id, timestamp, "ingest", body),
        hashlib.sha256,
    ).hexdigest()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/fleet/source-sync",
            "headers": [(b"x-runway-device-id", device.device_id.encode())],
        },
        receive,
    )
    with pytest.raises(HTTPException) as result:
        await validate_ingest_auth(
            request, signature, timestamp, session=session, purpose="source-sync"
        )
    assert result.value.status_code == 401
