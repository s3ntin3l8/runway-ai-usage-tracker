"""Enrollment and authentication helpers for paired sidecar identities."""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlmodel import Session, col, select

from app.core.encryption import DecryptionError, encryption_service
from app.models.db import SidecarDevice


def registry_id(device_id: str) -> str:
    return "device-" + device_id


def reserved_registry_id(value: str) -> bool:
    return re.fullmatch(r"device-[a-f0-9]{32}", value) is not None


# Source ids the server writes itself (the Claude cloud collector, `CLOUD_SIDECAR_ID`). A sidecar may
# not claim one: its usage would merge into the server's. A test pins that the two stay equal.
SERVER_SIDECAR_IDS = frozenset({"anthropic-cloud"})

# How long a freshly enrolled device may go without a source sync before that is worth a notice.
SOURCE_SYNC_GRACE = timedelta(minutes=10)

# The sidecar syncs on every push cycle (minutes), so an hour of silence from a device that is
# still ingesting means the route started failing after it once worked (a proxy change).
# This equals ``fleet_registry.STALE_THRESHOLD_MINUTES`` (what decides "online") on purpose: a
# device that has not pushed for that long reads as offline and gets no notice. Move them together.
SOURCE_SYNC_STALE = timedelta(hours=1)

SourceSyncState = Literal["ok", "never", "pending", "stale"]


def record_source_sync(session: Session, device_id: str) -> None:
    """Stamp an accepted ``source-sync`` request on the device (the caller commits)."""
    device = session.get(SidecarDevice, device_id)
    if device is not None:
        device.last_source_sync_at = datetime.now(UTC)
        session.add(device)


def source_sync_states(
    session: Session, registry_ids: Iterable[str], now: datetime | None = None
) -> dict[str, tuple[SourceSyncState, datetime | None]]:
    """Registry id -> (state, last accepted sync) for the enrolled devices among ``registry_ids``.

    ``never`` means the device is past its grace period and has not completed a ``source-sync``
    (a forward-auth proxy redirecting that route looks exactly like this); ``pending`` is the
    same inside the grace period; ``stale`` means it synced once but not for an hour, so the
    route broke afterwards. Whether the device is still online is the caller's to judge: an
    offline device is stale too. Legacy (unenrolled) and revoked devices are left out."""
    wanted = [
        registry_id.removeprefix("device-")
        for registry_id in registry_ids
        if reserved_registry_id(registry_id)
    ]
    if not wanted:
        return {}
    current = now or datetime.now(UTC)
    states: dict[str, tuple[SourceSyncState, datetime | None]] = {}
    for device in session.exec(
        select(SidecarDevice).where(col(SidecarDevice.device_id).in_(wanted))
    ).all():
        if device.revoked:
            continue
        state: SourceSyncState
        if device.last_source_sync_at is not None:
            state = "ok" if current - device.last_source_sync_at < SOURCE_SYNC_STALE else "stale"
        elif current - device.created_at < SOURCE_SYNC_GRACE:
            state = "pending"
        else:
            state = "never"
        states[registry_id(device.device_id)] = (state, device.last_source_sync_at)
    return states


def enroll(
    session: Session, hostname: str | None, *, platform: str | None = None
) -> tuple[SidecarDevice, str]:
    if not encryption_service.is_enabled:
        raise ValueError("Server encryption is required for device enrollment")
    secret = secrets.token_urlsafe(32)
    device = SidecarDevice(
        device_id=uuid.uuid4().hex,
        hostname=hostname,
        platform=platform if platform in {"Darwin", "Linux", "Windows"} else None,
        secret_encrypted=encryption_service.encrypt_string(secret),
    )
    session.add(device)
    session.flush()
    return device, secret


def authentication_key(session: Session, device_id: str) -> str | None:
    if not re.fullmatch(r"[a-f0-9]{32}", device_id) or not encryption_service.is_enabled:
        return None
    device = session.get(SidecarDevice, device_id)
    if device is None or device.revoked or not device.secret_encrypted.startswith("gAAAAA"):
        return None
    try:
        return encryption_service.decrypt_string(device.secret_encrypted)
    except DecryptionError:
        return None


def request_authentication_key(session: Session | None, device_id: str) -> str | None:
    """Finish the authentication read before the independent replay receipt commits.

    Keeping that read in the handler's transaction would pin a WAL snapshot
    from before the receipt, preventing the subsequent source write from
    acquiring its reservation. Never commit or roll back handler-owned work.

    Production uses independent pooled connections. Single-connection test
    binds (e.g. StaticPool) share the physical transaction: fixtures must commit
    setup before authentication, because closing this session rolls it back.
    """
    if session is None:
        return None
    with Session(session.get_bind()) as auth_session:
        return authentication_key(auth_session, device_id)


def message(device_id: str, timestamp: str, purpose: str, body: bytes) -> bytes:
    """Domain-bind signatures without depending on reverse-proxy path rewriting."""
    return f"DEVICEv1|{device_id}|{timestamp}|{purpose}|".encode() + body


def authenticated_registry_id(request: Any, claimed: str | None) -> str | None:
    """Legacy HMAC holders cannot claim the reserved enrolled-device namespace."""
    from fastapi import HTTPException

    device_id = getattr(request.state, "sidecar_device_id", None)
    if device_id is not None:
        return registry_id(device_id)
    if claimed is not None and reserved_registry_id(claimed):
        raise HTTPException(
            status_code=403, detail="Device identity requires device authentication"
        )
    if claimed is not None and claimed in SERVER_SIDECAR_IDS:
        raise HTTPException(status_code=403, detail="This sidecar id is reserved for the server")
    return claimed
