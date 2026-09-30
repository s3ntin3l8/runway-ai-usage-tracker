"""Persistence helpers for non-secret credential source metadata."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime

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
) -> CredentialSource:
    """Create or refresh a source without replacing operator preferences.

    Initial priority is assigned only when creating a row. Later refreshes
    preserve the operator's enabled state and priority. Refreshing metadata does
    not reset health; only a collection result confirms credential health.
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
            priority=priority,
        )
        session.add(row)
    else:
        row.source_type = source_type
        row.source_label = source_label
        row.credential_origin = credential_origin
        row.sidecar_id = sidecar_id
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
