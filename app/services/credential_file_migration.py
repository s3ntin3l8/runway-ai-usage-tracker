"""Durable, installation-wide notice for confirmed credential-file removals."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from app.core.registry import registry
from app.models.db import SystemConfig
from app.models.schemas import AffectedCredentialProvider, CredentialFileMigrationNotice


class _MigrationRecord(BaseModel):
    provider_ids: list[str]
    removed_count: int = Field(gt=0)
    recorded_at: str
    dismissed_at: str | None = None


def _read(row: SystemConfig | None) -> _MigrationRecord | None:
    if row is None or not row.credential_file_migration_json:
        return None
    try:
        return _MigrationRecord.model_validate_json(row.credential_file_migration_json)
    except ValidationError:
        return None


def record_file_source_removal(session: Session, count: int, provider_ids: tuple[str, ...]) -> None:
    """Stage the record in the caller's removal transaction; never commit here."""
    if not count:
        return
    row = session.exec(select(SystemConfig)).first()
    previous = _read(row)
    record = _MigrationRecord(
        provider_ids=sorted(set(provider_ids) | set(previous.provider_ids if previous else [])),
        removed_count=count + (previous.removed_count if previous else 0),
        recorded_at=previous.recorded_at if previous else datetime.now(UTC).isoformat(),
        dismissed_at=previous.dismissed_at if previous else None,
    )
    if row is None:
        row = SystemConfig()
    row.credential_file_migration_json = record.model_dump_json()
    session.add(row)


def get_file_migration_notice(session: Session) -> CredentialFileMigrationNotice | None:
    record = _read(session.exec(select(SystemConfig)).first())
    if record is None or record.dismissed_at:
        return None
    return CredentialFileMigrationNotice(
        providers=[
            AffectedCredentialProvider(
                provider_id=provider_id,
                name=str(registry.get_provider(provider_id).get("name") or provider_id),
            )
            for provider_id in record.provider_ids
        ],
        removed_count=record.removed_count,
        recorded_at=record.recorded_at,
    )


def dismiss_file_migration_notice(session: Session) -> None:
    row = session.exec(select(SystemConfig)).first()
    record = _read(row)
    if row is not None and record is not None and not record.dismissed_at:
        record.dismissed_at = datetime.now(UTC).isoformat()
        row.credential_file_migration_json = record.model_dump_json()
        session.add(row)
