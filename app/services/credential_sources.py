"""Persistence helpers for non-secret credential source metadata."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, select

from app.models.db import CredentialSource
from app.services.account_identity import canonical_account_id


def describe_origin(origin: str | None) -> tuple[str, str]:
    """Return a UI-safe source kind and label for a sidecar credential origin."""
    value = origin or "sidecar"
    if value.startswith("env:"):
        return "env", value.removeprefix("env:")
    if value.startswith("path:") or value.startswith("file:"):
        return "file", os.path.basename(
            value.split(":", 1)[1].split("#", 1)[0]
        ) or "Credential file"
    if value.startswith("cookie:"):
        return "sidecar", "Browser cookie"
    return "sidecar", "Sidecar credential"


def sidecar_source_id(sidecar_id: str, origin: str | None) -> str:
    """Stable source identity, scoped to the machine that reported it."""
    digest = hashlib.sha256(f"{sidecar_id}\0{origin or 'legacy'}".encode()).hexdigest()[:24]
    return f"sidecar:{digest}"


def is_sidecar_source(source: Mapping[str, Any]) -> bool:
    """Whether a credential source was reported by a sidecar.

    ``source_type`` describes the credential itself (file, env, cookie), not
    where it was observed. Sidecar ownership is established by its id and
    origin metadata.
    """
    return bool(source.get("sidecar_id") and source.get("credential_origin"))


# Sentinel: "the caller has no opinion" — distinct from an explicit ``None``
# (a credential that genuinely has no expiry).
UNSET: Any = object()


def resolve_source_account(session: Session, provider_id: str, source_id: str) -> str | None:
    """The account an already-registered source belongs to, or ``None`` if unknown.

    A ``source_id`` (sidecar + origin) names exactly one credential, so it maps to one
    account. Prefer a real identity over the ``default`` placeholder, and the most
    recently seen row when several exist.
    """
    rows = session.exec(
        select(CredentialSource)
        .where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.source_id == source_id,
        )
        .order_by(col(CredentialSource.last_seen).desc())
    ).all()
    for row in rows:
        if row.account_id != "default":
            return row.account_id
    return rows[0].account_id if rows else None


def touch_source(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    source_id: str,
    source_type: str,
    source_label: str,
    credential_origin: str | None = None,
    sidecar_id: str | None = None,
    credential_expires_at: datetime | None = UNSET,
    token_types: list[str] | None = UNSET,
) -> CredentialSource:
    """Create or refresh a source without replacing operator preferences.

    Initial priority is assigned only when creating a row. Later refreshes
    preserve the operator's enabled state and priority. Refreshing metadata does
    not reset health; only a collection result confirms credential health.

    ``credential_expires_at`` / ``token_types`` are overwritten only when the caller
    passes them (``None`` is a real value: "no expiry"). A caller that doesn't know
    them — ``/fleet/ingest`` sees the secrets but the manifest reports the health
    metadata — must not wipe what the other recorded.
    """
    aid = canonical_account_id(account_id)
    row = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.account_id == aid,
            CredentialSource.source_id == source_id,
        )
    ).first()
    if row is None:
        count = session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.account_id == aid,
            )
        ).all()
        # New discovered sources take the next slot so they follow existing
        # operator-configured and sidecar sources unless config claims slot 0.
        priority = max((item.priority for item in count), default=-1) + 1
        if source_type == "config":
            for item in count:
                item.priority += 1
                session.add(item)
            priority = 0
        row = CredentialSource(
            provider_id=provider_id,
            account_id=aid,
            source_id=source_id,
            source_type=source_type,
            source_label=source_label,
            credential_origin=credential_origin,
            sidecar_id=sidecar_id,
            credential_expires_at=None if credential_expires_at is UNSET else credential_expires_at,
            token_types_json=json.dumps([] if token_types is UNSET else token_types or []),
            priority=priority,
        )
        session.add(row)
    else:
        row.source_type = source_type
        row.source_label = source_label
        row.credential_origin = credential_origin
        row.sidecar_id = sidecar_id
        if credential_expires_at is not UNSET:
            row.credential_expires_at = credential_expires_at
        if token_types is not UNSET:
            row.token_types_json = json.dumps(token_types or [])
        row.last_seen = datetime.now(UTC)
    session.flush()
    return row


def account_sources(session: Session, provider_id: str, account_id: str) -> list[CredentialSource]:
    return list(
        session.exec(
            select(CredentialSource)
            .where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.account_id == canonical_account_id(account_id),
            )
            .order_by(col(CredentialSource.priority), col(CredentialSource.id))
        ).all()
    )


def record_source_health(
    session: Session, provider_id: str, account_id: str, source_id: str, health: str
) -> None:
    row = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.account_id == canonical_account_id(account_id),
            CredentialSource.source_id == source_id,
        )
    ).first()
    if row:
        row.health = health
        row.health_detail = {
            "auth_failed": "Authentication failed",
            "unavailable": "Collection failed",
            "degraded": "Some requests were rejected; quota was collected",
        }.get(health)
        session.add(row)
