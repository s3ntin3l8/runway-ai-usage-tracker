"""Service for managing the persistent sidecar fleet registry."""

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from app.core.utils import scrub_log
from app.models.db import SidecarRegistry

logger = logging.getLogger(__name__)

# Sidecars that haven't checked in for this long are considered stale
_STALE_THRESHOLD_MINUTES = 60


class FleetRegistryService:
    """Manages upsert and CRUD operations for SidecarRegistry rows."""

    def __init__(self) -> None:
        # In-memory set of sidecar IDs awaiting a "collect now" trigger.
        # The flag is consumed (cleared) the first time the sidecar polls after it is set.
        self._pending_triggers: set[str] = set()

        # In-memory tracking of the last time each sidecar was asked to poll a specific provider.
        # Key: sidecar_id -> dict(provider_id -> unix_timestamp)
        self._last_provider_polls: dict[str, dict[str, float]] = {}

    def get_due_providers(
        self, sidecar_id: str, enabled_providers: list[tuple[str, int]]
    ) -> tuple[list[str], bool]:
        """
        Compare current time against last poll history to see which providers are due.
        enabled_providers: list of (provider_id, interval_seconds)
        Returns (list_of_due_providers, trigger_consumed_bool)
        """
        import time

        now = time.time()
        due = []
        sidecar_history = self._last_provider_polls.setdefault(sidecar_id, {})
        trigger_consumed = self.consume_pending_trigger(sidecar_id)

        # Special case: if a global trigger is pending, poll everything
        if trigger_consumed:
            for p_id, _ in enabled_providers:
                due.append(p_id)
                sidecar_history[p_id] = now
            return due, trigger_consumed

        for p_id, interval in enabled_providers:
            last = sidecar_history.get(p_id, 0.0)
            if now - last >= interval:
                due.append(p_id)
                sidecar_history[p_id] = now

        return due, trigger_consumed

    def set_pending_trigger(self, sidecar_id: str) -> None:
        """Schedule an immediate collection cycle for the given sidecar."""
        self._pending_triggers.add(sidecar_id)
        logger.info(f"Remote trigger queued for sidecar '{sidecar_id}'")

    def consume_pending_trigger(self, sidecar_id: str) -> bool:
        """Return True (and clear the flag) if a trigger is pending for this sidecar."""
        if sidecar_id in self._pending_triggers:
            self._pending_triggers.discard(sidecar_id)
            logger.info(f"Remote trigger delivered to sidecar '{sidecar_id}'")
            return True
        return False

    def upsert_sidecar(
        self,
        sidecar_id: str,
        source_ip: str,
        session: Session,
        sidecar_version: str | None = None,
        os_platform: str | None = None,
        collection_errors: int = 0,
        last_log_lines: list[str] | None = None,
    ) -> SidecarRegistry:
        """Insert on first sight; update last_seen and ingest_count on repeat calls."""
        row = session.get(SidecarRegistry, sidecar_id)
        if row:
            row.last_seen = datetime.now(UTC)
            row.ingest_count += 1
            row.last_ip = source_ip
            if sidecar_version is not None:
                row.sidecar_version = sidecar_version
            if os_platform is not None:
                row.os_platform = os_platform
            if collection_errors > 0:
                row.error_count += collection_errors
            if last_log_lines is not None:
                row.recent_logs = json.dumps(last_log_lines[-20:])
            logger.debug(f"Updated sidecar '{sidecar_id}' (ingest #{row.ingest_count})")
        else:
            row = SidecarRegistry(
                sidecar_id=sidecar_id,
                hostname=sidecar_id,
                last_ip=source_ip,
                sidecar_version=sidecar_version,
                os_platform=os_platform,
                error_count=collection_errors,
                recent_logs=json.dumps(last_log_lines[-20:]) if last_log_lines else None,
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
        logger.info(f"Deleted sidecar from registry: '{scrub_log(sidecar_id)}'")
        return True

    def to_dict(self, row: SidecarRegistry) -> dict:
        """Serialize a SidecarRegistry row to a response dict."""
        # Imported lazily so unit tests that exercise to_dict don't need
        # the global FastAPI startup wiring.
        from app.services.sidecar_version_checker import (
            is_update_available,
            parse_channel,
            sidecar_version_checker,
        )

        last_seen_utc = row.last_seen.replace(tzinfo=UTC)
        stale = last_seen_utc < datetime.now(UTC) - timedelta(minutes=_STALE_THRESHOLD_MINUTES)
        latest_version = sidecar_version_checker.get_latest()
        latest_edge_sha = sidecar_version_checker.get_latest_edge_sha()
        return {
            "sidecar_id": row.sidecar_id,
            "hostname": row.hostname,
            "custom_name": row.custom_name,
            "tags": row.tags,
            "last_seen": last_seen_utc.isoformat(),
            "first_seen": row.first_seen.replace(tzinfo=UTC).isoformat(),
            "last_ip": row.last_ip,
            "error_count": row.error_count,
            "ingest_count": row.ingest_count,
            "sidecar_version": row.sidecar_version,
            "latest_version": latest_version,
            "channel": parse_channel(row.sidecar_version)[0],
            "update_available": is_update_available(
                row.sidecar_version, latest_version, latest_edge_sha
            ),
            "os_platform": row.os_platform,
            "collection_enabled": row.collection_enabled,
            "stale": stale,
            "stale_threshold_minutes": _STALE_THRESHOLD_MINUTES,
            "recent_logs": json.loads(row.recent_logs) if row.recent_logs else [],
        }


fleet_registry = FleetRegistryService()
