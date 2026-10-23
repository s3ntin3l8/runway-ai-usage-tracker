"""Enrollment and authentication helpers for paired sidecar identities."""

from __future__ import annotations

import re
import secrets
import uuid
from typing import Any

from sqlmodel import Session

from app.core.encryption import DecryptionError, encryption_service
from app.models.db import SidecarDevice


def registry_id(device_id: str) -> str:
    return "device-" + device_id


def reserved_registry_id(value: str) -> bool:
    return re.fullmatch(r"device-[a-f0-9]{32}", value) is not None


def enroll(session: Session, hostname: str | None) -> tuple[SidecarDevice, str]:
    if not encryption_service.is_enabled:
        raise ValueError("Server encryption is required for device enrollment")
    secret = secrets.token_urlsafe(32)
    device = SidecarDevice(
        device_id=uuid.uuid4().hex,
        hostname=hostname,
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
    return claimed
