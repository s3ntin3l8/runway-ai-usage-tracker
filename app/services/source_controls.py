"""Durable device/source consent, revocation and encrypted credential contributions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.encryption import DecryptionError, encryption_service
from app.models.db import (
    SidecarDevice,
    SidecarSourceCommand,
    SidecarSourceContribution,
    SidecarSourceRevision,
    SidecarSourceState,
)
from app.models.source_controls import ScraperReport
from app.services import sidecar_devices
from app.services.credential_sources import sidecar_source_id
from scripts.sidecar_pkg.source_catalog import catalog, source_for_origin

_ENABLED = False


def _write_lock(session: Session) -> None:
    """Serialize consent decisions before reads, including SQLite legacy transaction mode."""
    connection = session.connection()
    native = connection.connection.driver_connection
    if native is None:
        raise ValueError("Source transaction unavailable")
    if connection.dialect.name == "sqlite" and not native.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def state(session: Session, device_id: str, source_id: str) -> SidecarSourceState | None:
    return session.exec(
        select(SidecarSourceState)
        .where(SidecarSourceState.device_id == device_id, SidecarSourceState.source_id == source_id)
        .execution_options(populate_existing=True)
    ).first()


def bump(session: Session) -> None:
    session.execute(
        text("INSERT OR IGNORE INTO sidecar_source_revision (id, revision) VALUES (1, 0)")
    )
    session.execute(text("UPDATE sidecar_source_revision SET revision = revision + 1 WHERE id = 1"))


def _withdraw(session: Session, device_id: str, source_id: str) -> None:
    for row in session.exec(
        select(SidecarSourceContribution).where(
            SidecarSourceContribution.device_id == device_id,
            SidecarSourceContribution.scraper_id == source_id,
        )
    ).all():
        row.payload_encrypted = None
        session.add(row)
    bump(session)


def apply_report(
    session: Session, device_id: str, definition: dict[str, Any]
) -> SidecarSourceState:
    _write_lock(session)
    report = ScraperReport.model_validate(definition)
    descriptor = catalog().get(report.source_id)
    if descriptor is None or descriptor.provider != report.provider_id:
        raise ValueError("Unsupported scraper")
    if session.get(SidecarDevice, device_id) is None:
        raise ValueError("Unknown device")
    row = state(session, device_id, report.source_id)
    if row is None:
        row = SidecarSourceState(
            device_id=device_id,
            source_id=report.source_id,
            provider_id=report.provider_id,
            label=descriptor.label,
            revision=report.revision,
        )
        session.add(row)
    elif report.generation < row.generation:
        return row
    previous_tombstone = row.revoked_through
    changed = (row.local_read, row.generation, row.revision) != (
        report.read,
        report.generation,
        report.revision,
    )
    row.generation, row.revision = report.generation, report.revision
    row.local_read = report.read
    row.label = descriptor.label
    row.last_report_at = datetime.now(UTC)
    previous_report = json.loads(row.report_json)
    reported = report.model_dump()
    for field in ("last_read_at", "last_renewed_at"):
        reported[field] = reported[field] or previous_report.get(field)
    row.report_json = json.dumps({**reported, "label": descriptor.label})
    if not report.read:
        row.revoked_through = max(row.revoked_through, report.generation)
        if changed or row.revoked_through != previous_tombstone:
            _withdraw(session, device_id, report.source_id)
    elif changed:
        bump(session)
    session.add(row)
    session.flush()
    return row


def allowed(session: Session, device_id: str, source_id: str, generation: int) -> bool:
    device = session.get(SidecarDevice, device_id, populate_existing=True)
    row = state(session, device_id, source_id)
    descriptor = catalog().get(source_id)
    return bool(
        descriptor is not None
        and row is not None
        and row.revision == descriptor.revision
        and device is not None
        and not device.revoked
        and row is not None
        and row.local_read
        and not row.remote_paused
        and generation == row.generation
        and generation > row.revoked_through
    )


def save_contribution(
    session: Session,
    device_id: str,
    definition: dict[str, Any] | None,
    *,
    provider_id: str,
    origin: str,
    account_id: str,
    tokens: dict[str, str],
    metadata: dict[str, Any],
) -> SidecarSourceContribution:
    global _ENABLED
    _write_lock(session)
    if not encryption_service.is_enabled:
        raise ValueError("Server encryption required")
    if definition is not None and not isinstance(definition, dict):
        raise ValueError("Source contribution not authorized")
    device = session.get(SidecarDevice, device_id)
    proof = definition or {}
    scraper_id = proof.get("source_id")
    generation = proof.get("generation", 0)
    if type(generation) is not int:
        raise ValueError("Source contribution not authorized")
    expected_scraper = source_for_origin(provider_id, origin)
    source = state(session, device_id, scraper_id) if isinstance(scraper_id, str) else None
    if (
        device is None
        or device.revoked
        or expected_scraper != scraper_id
        or (
            scraper_id is not None
            and (
                source is None
                or source.provider_id != provider_id
                or source.revision != proof.get("revision")
                or type(generation) is not int
                or not allowed(session, device_id, scraper_id, generation)
            )
        )
    ):
        raise ValueError("Source contribution not authorized")
    credential_source_id = sidecar_source_id(sidecar_devices.registry_id(device_id), origin)
    row = session.exec(
        select(SidecarSourceContribution).where(
            SidecarSourceContribution.device_id == device_id,
            SidecarSourceContribution.provider_id == provider_id,
            SidecarSourceContribution.credential_source_id == credential_source_id,
        )
    ).first()
    if row is None:
        row = SidecarSourceContribution(
            device_id=device_id,
            scraper_id=scraper_id,
            provider_id=provider_id,
            credential_source_id=credential_source_id,
            origin=origin,
            account_id=account_id,
            generation=generation,
        )
    if row.payload_encrypted and row.account_id == account_id and row.generation == generation:
        from app.services.token_cache import TokenCache

        previous = encryption_service.decrypt_json(row.payload_encrypted)
        if (
            isinstance(previous, dict)
            and isinstance(previous.get("tokens"), dict)
            and TokenCache._is_staler(tokens, previous["tokens"])
        ):
            return row
    row.scraper_id, row.generation, row.account_id = scraper_id, generation, account_id
    safe_metadata = {
        **metadata,
        "durable_source": True,
        "device_id": device_id,
        "scraper_id": scraper_id,
        "consent_generation": generation,
        "source_id": credential_source_id,
        "sidecar_id": sidecar_devices.registry_id(device_id),
        "source": sidecar_devices.registry_id(device_id),
    }
    row.payload_encrypted = encryption_service.encrypt_json(
        {"tokens": tokens, "metadata": safe_metadata}
    )
    session.add(row)
    session.flush()
    bump(session)
    _ENABLED = True
    return row


def queue_command(
    session: Session, device_id: str, source_id: str, action: str
) -> SidecarSourceCommand:
    _write_lock(session)
    row = state(session, device_id, source_id)
    if row is None or action not in {"pause", "resume", "revoke", "request_setup", "dismiss"}:
        raise ValueError("Unknown scraper or action")
    if action == "resume" and (not row.local_read or row.generation <= row.revoked_through):
        raise ValueError("Resume requires local consent")
    command = SidecarSourceCommand(
        device_id=device_id, source_id=source_id, generation=row.generation, action=action
    )
    if action == "pause":
        row.remote_paused = True
        _withdraw(session, device_id, source_id)
    elif action == "revoke":
        row.revoked_through = max(row.revoked_through, row.generation)
        _withdraw(session, device_id, source_id)
    elif action == "dismiss":
        descriptor = catalog().get(source_id)
        row.dismissed_revision = descriptor.revision if descriptor else row.revision
    session.add(row)
    session.add(command)
    session.flush()
    return command


def pending_commands(session: Session, device_id: str) -> list[dict[str, Any]]:
    rows = session.exec(
        select(SidecarSourceCommand)
        .where(
            SidecarSourceCommand.device_id == device_id,
            col(SidecarSourceCommand.acknowledged).is_(None),
        )
        .order_by(col(SidecarSourceCommand.id))
    ).all()  # noqa: E711
    return [
        {
            "id": row.id,
            "source_id": row.source_id,
            "generation": row.generation,
            "action": row.action,
        }
        for row in rows
    ]


def acknowledge(session: Session, device_id: str, command_id: int, status: str) -> None:
    _write_lock(session)
    command = session.get(SidecarSourceCommand, command_id)
    if (
        command is None
        or command.device_id != device_id
        or status not in {"applied", "denied", "superseded"}
    ):
        raise ValueError("Unknown command")
    if command.acknowledged is not None:
        return
    # A later administrative action always wins over a delayed older resume.
    newer = session.exec(
        select(SidecarSourceCommand).where(
            SidecarSourceCommand.device_id == device_id,
            SidecarSourceCommand.source_id == command.source_id,
            col(SidecarSourceCommand.id) > command_id,
        )
    ).first()
    row = state(session, device_id, command.source_id)
    if (
        command.action == "resume"
        and status == "applied"
        and newer is None
        and row is not None
        and row.local_read
        and row.generation == command.generation
        and row.generation > row.revoked_through
    ):
        row.remote_paused = False
        session.add(row)
        bump(session)
    command.acknowledged = status
    session.add(command)
    session.flush()


def snapshot(session: Session) -> tuple[int, list[dict[str, Any]]]:
    current_revision = revision(session)
    result = []
    if encryption_service.is_enabled:
        for row in session.exec(
            select(SidecarSourceContribution).where(
                col(SidecarSourceContribution.payload_encrypted).is_not(None)
            )
        ).all():  # noqa: E711
            device = session.get(SidecarDevice, row.device_id)
            if (
                device is None
                or device.revoked
                or (
                    row.scraper_id is not None
                    and not allowed(session, row.device_id, row.scraper_id, row.generation)
                )
            ):
                continue
            try:
                if not row.payload_encrypted or not row.payload_encrypted.startswith("gAAAAA"):
                    continue
                value = encryption_service.decrypt_json(row.payload_encrypted)
                if (
                    not isinstance(value, dict)
                    or not isinstance(value.get("tokens"), dict)
                    or not all(
                        isinstance(k, str) and isinstance(v, str)
                        for k, v in value["tokens"].items()
                    )
                    or not isinstance(value.get("metadata"), dict)
                ):
                    continue
                result.append(
                    {
                        "provider_id": row.provider_id,
                        "account_id": row.account_id,
                        "source_id": row.credential_source_id,
                        **value,
                    }
                )
            except (DecryptionError, ValueError, TypeError):
                continue
    return current_revision, result


def initialize() -> None:
    global _ENABLED

    # Every worker must observe contributions first enrolled by another worker.
    _ENABLED = True


def revision(session: Session) -> int:
    return (
        session.exec(
            select(SidecarSourceRevision.revision).where(SidecarSourceRevision.id == 1)
        ).first()
        or 0
    )


def read_snapshot(previous_revision: int) -> tuple[int, list[dict[str, Any]]] | None:
    """Read committed lifecycle changes; silence never expires an enrolled source."""
    if not _ENABLED:
        return None
    from app.core.db import engine

    with Session(engine) as session:
        if revision(session) == previous_revision:
            return None
        return snapshot(session)


def contribution_proof(
    session: Session, device_id: str, provider_id: str, origin: str, proof: Any
) -> dict[str, Any] | None:
    """Validate source binding before any credential metadata mutation."""
    if not isinstance(provider_id, str) or not isinstance(origin, str) or len(origin) > 4096:
        raise ValueError("Source contribution not authorized")
    _write_lock(session)
    expected = source_for_origin(provider_id, origin)
    if expected is None:
        if proof is not None:
            raise ValueError("Source contribution not authorized")
        return None
    if not isinstance(proof, dict) or set(proof) != {"source_id", "revision", "generation"}:
        raise ValueError("Source contribution not authorized")
    row = state(session, device_id, expected)
    if (
        proof.get("source_id") != expected
        or type(proof.get("generation")) is not int
        or row is None
        or row.provider_id != provider_id
        or row.revision != proof.get("revision")
        or not allowed(session, device_id, expected, proof["generation"])
    ):
        raise ValueError("Source contribution not authorized")
    return proof


def promote_cached_source(
    provider_id: str, source_id: str, from_id: str, to_id: str, expected_tokens: dict[str, str]
) -> bool:
    """Persist a verified identity without moving credentials into a merged cache."""
    from app.core.db import engine

    with Session(engine) as session:
        _write_lock(session)
        row = session.exec(
            select(SidecarSourceContribution).where(
                SidecarSourceContribution.provider_id == provider_id,
                SidecarSourceContribution.credential_source_id == source_id,
                SidecarSourceContribution.account_id == from_id,
            )
        ).first()
        if (
            row is None
            or row.payload_encrypted is None
            or (
                row.scraper_id is not None
                and not allowed(session, row.device_id, row.scraper_id, row.generation)
            )
        ):
            return False
        value = encryption_service.decrypt_json(row.payload_encrypted)
        if not isinstance(value, dict) or value.get("tokens") != expected_tokens:
            return False
        row.account_id = to_id
        value["metadata"]["identity_pending"] = False
        row.payload_encrypted = encryption_service.encrypt_json(value)
        session.add(row)
        bump(session)
        session.commit()
        return True


def replace_cached_source(
    provider_id: str,
    source_id: str,
    account_id: str,
    expected_tokens: dict[str, str],
    tokens: dict[str, str],
) -> bool:
    """Save an already-selected replacement only while its original grant is eligible."""
    from app.core.db import engine

    with Session(engine) as session:
        row = session.exec(
            select(SidecarSourceContribution).where(
                SidecarSourceContribution.provider_id == provider_id,
                SidecarSourceContribution.credential_source_id == source_id,
                SidecarSourceContribution.account_id == account_id,
            )
        ).first()
        if row is None or row.payload_encrypted is None:
            return False
        device = session.get(SidecarDevice, row.device_id)
        if (
            device is None
            or device.revoked
            or (
                row.scraper_id is not None
                and not allowed(session, row.device_id, row.scraper_id, row.generation)
            )
        ):
            return False
        value = encryption_service.decrypt_json(row.payload_encrypted)
        if not isinstance(value, dict) or value.get("tokens") != expected_tokens:
            return False
        if provider_id == "anthropic" and expected_tokens.get("oauth_read_only") == "1":
            return False
        value["tokens"] = tokens
        row.payload_encrypted = encryption_service.encrypt_json(value)
        session.add(row)
        bump(session)
        session.commit()
        return True


def revoke_device(session: Session, device_id: str) -> None:
    """Removing an enrolled device also ends its authorization and contributions."""
    _write_lock(session)
    device = session.get(SidecarDevice, device_id)
    if device is None:
        return
    device.revoked = True
    session.add(device)
    for row in session.exec(
        select(SidecarSourceContribution).where(SidecarSourceContribution.device_id == device_id)
    ).all():
        row.payload_encrypted = None
        session.add(row)
    bump(session)


def current_controls(session: Session, device_id: str) -> list[dict[str, Any]]:
    rows = session.exec(
        select(SidecarSourceState).where(SidecarSourceState.device_id == device_id)
    ).all()
    return [
        {
            "source_id": row.source_id,
            "paused": row.remote_paused,
            "revoked_through": row.revoked_through,
        }
        for row in rows
    ]
