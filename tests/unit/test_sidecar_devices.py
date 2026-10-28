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
    session.commit()
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
    session.commit()
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


def test_source_sync_state_is_pending_never_or_ok(session):
    from datetime import UTC, datetime, timedelta

    device, _ = sidecar_devices.enroll(session, "mac")
    registry = sidecar_devices.registry_id(device.device_id)
    now = datetime.now(UTC)

    def state(at):
        return sidecar_devices.source_sync_states(session, [registry], now=at)[registry]

    # Inside the grace period a missing sync is not yet worth a notice.
    assert state(now)[0] == "pending"
    # Past it, a device that never synced reads "never" (e.g. a proxy redirecting the route).
    later = now + sidecar_devices.SOURCE_SYNC_GRACE + timedelta(seconds=1)
    assert state(later) == ("never", None)
    # A recorded sync wins, whatever the age.
    sidecar_devices.record_source_sync(session, device.device_id)
    session.commit()
    session.refresh(device)
    result = state(later)
    assert result[0] == "ok" and result[1] == device.last_source_sync_at


def test_source_sync_state_skips_legacy_and_revoked(session):
    device, _ = sidecar_devices.enroll(session, "mac")
    device.revoked = True
    session.add(device)
    session.commit()
    registry = sidecar_devices.registry_id(device.device_id)
    assert sidecar_devices.source_sync_states(session, ["desktop", registry]) == {}


def test_source_sync_state_goes_stale_when_syncing_stops(session):
    from datetime import UTC, datetime, timedelta

    device, _ = sidecar_devices.enroll(session, "mac")
    registry = sidecar_devices.registry_id(device.device_id)
    now = datetime.now(UTC)
    device.last_source_sync_at = now - timedelta(minutes=59)
    session.add(device)
    session.commit()

    def state():
        return sidecar_devices.source_sync_states(session, [registry], now=now)[registry][0]

    assert state() == "ok"
    device.last_source_sync_at = now - sidecar_devices.SOURCE_SYNC_STALE - timedelta(seconds=1)
    session.add(device)
    session.commit()
    # A device that synced days ago must not read as healthy: the route broke afterwards.
    assert state() == "stale"


def test_deferred_columns_includes_last_source_sync_at():
    from app.core.db import _DEFERRED_COLUMNS

    assert ("sidecar_devices", "last_source_sync_at", "DATETIME") in _DEFERRED_COLUMNS
