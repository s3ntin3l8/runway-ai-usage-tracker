"""Service for managing the persistent sidecar fleet registry."""

import logging
from datetime import UTC, datetime

from sqlmodel import Session

from app.models.db import SidecarRegistry

logger = logging.getLogger(__name__)


class FleetRegistryService:
    """Manages upsert and CRUD operations for SidecarRegistry rows."""

    def upsert_sidecar(self, sidecar_id: str, source_ip: str, session: Session) -> SidecarRegistry:
        """Insert on first sight; update last_seen and ingest_count on repeat calls."""
        row = session.get(SidecarRegistry, sidecar_id)
        if row:
            row.last_seen = datetime.now(UTC)
            row.ingest_count += 1
            row.last_ip = source_ip
            logger.debug(f"Updated sidecar '{sidecar_id}' (ingest #{row.ingest_count})")
        else:
            row = SidecarRegistry(
                sidecar_id=sidecar_id,
                hostname=sidecar_id,
                last_ip=source_ip,
            )
            session.add(row)
            logger.info(f"Registered new sidecar: '{sidecar_id}' from {source_ip}")
        session.commit()
        session.refresh(row)
        return row

    def update_sidecar(
        self,
        sidecar_id: str,
        custom_name: str | None,
        tags: list[str] | None,
        session: Session,
    ) -> SidecarRegistry | None:
        """Update custom_name and/or tags. Returns None if sidecar not found."""
        row = session.get(SidecarRegistry, sidecar_id)
        if not row:
            return None
        if custom_name is not None:
            row.custom_name = custom_name
        if tags is not None:
            row.tags = tags
        session.commit()
        session.refresh(row)
        return row

    def delete_sidecar(self, sidecar_id: str, session: Session) -> bool:
        """Remove sidecar from registry. Returns True if deleted, False if not found."""
        row = session.get(SidecarRegistry, sidecar_id)
        if not row:
            return False
        session.delete(row)
        session.commit()
        logger.info(f"Deleted sidecar from registry: '{sidecar_id}'")
        return True

    def to_dict(self, row: SidecarRegistry) -> dict:
        """Serialize a SidecarRegistry row to a response dict."""
        return {
            "sidecar_id": row.sidecar_id,
            "hostname": row.hostname,
            "custom_name": row.custom_name,
            "tags": row.tags,
            "last_seen": row.last_seen.replace(tzinfo=UTC).isoformat(),
            "first_seen": row.first_seen.replace(tzinfo=UTC).isoformat(),
            "last_ip": row.last_ip,
            "error_count": row.error_count,
            "ingest_count": row.ingest_count,
        }


fleet_registry = FleetRegistryService()
