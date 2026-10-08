"""Service for managing the persistent sidecar fleet registry."""

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.core.log_redaction import redact_secrets
from app.core.utils import scrub_log
from app.models.db import CredentialSource, SidecarRegistry
from app.services.refresh_policy import parse_provider_flags

logger = logging.getLogger(__name__)


_LOG_LINE_LIMIT = 2000


def _recent_logs_json(lines: list[str]) -> str:
    """The last 20 reported log lines as stored JSON, with credential-shaped text redacted.

    The sidecar redacts before sending, but a sidecar is not a trust boundary: an
    older version, or a compromised host, could forward a secret into a column
    that the fleet API returns to every reader.
    """
    # Capped first: the redaction regexes must never see unbounded input.
    return json.dumps([str(redact_secrets(str(line)[:_LOG_LINE_LIMIT])) for line in lines[-20:]])


# Sidecars that haven't checked in for this long are considered stale
STALE_THRESHOLD_MINUTES = 60

UPDATE_CHANNELS = ("stable", "beta", "edge")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def effective_update_channel(row: SidecarRegistry, fleet_channel: str | None) -> str:
    """Channel this sidecar is told to follow: its own override, else the fleet's."""
    return row.update_channel_desired or fleet_channel or "stable"


def effective_auto_update(row: SidecarRegistry, fleet_auto_update: bool | None) -> bool:
    """Whether this sidecar is told to auto-update: its own override, else the fleet's."""
    if row.auto_update_desired is not None:
        return row.auto_update_desired
    return bool(fleet_auto_update)


class FleetRegistryService:
    """Manages upsert and CRUD operations for SidecarRegistry rows."""

    def __init__(self) -> None:
        # In-memory set of sidecar IDs awaiting a "collect now" trigger.
        # The flag is consumed (cleared) the first time the sidecar polls after it is set.
        self._pending_triggers: set[str] = set()

        # NOTE: the one-shot "update now" push used to live here as an in-memory
        # set, but that meant a queued update was silently dropped by a server
        # restart between the admin click and the sidecar's next check-in — with
        # a dead/slow sidecar that window can be long. It's now the persisted
        # `SidecarRegistry.pending_update` column instead; see
        # set_pending_update / consume_pending_update below.

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

    def set_pending_update(self, sidecar_id: str, session: Session) -> SidecarRegistry | None:
        """Schedule a one-shot self-update for the given sidecar.

        Persisted on the registry row (not in-memory) so the intent survives a
        server restart between the admin click and the sidecar's next
        check-in. Returns None if the sidecar isn't registered yet — mirrors
        `update_sidecar`; the caller (endpoint) turns that into a 404, same as
        `_set_sidecar_collection_enabled` does for pause/resume.
        """
        row = session.get(SidecarRegistry, sidecar_id)
        if not row:
            return None
        row.pending_update = True
        session.commit()
        session.refresh(row)
        logger.info(f"Remote update queued for sidecar '{scrub_log(sidecar_id)}'")
        return row

    def consume_pending_update(self, sidecar_id: str, session: Session) -> bool:
        """Return True (and clear the flag) if an update push is pending for this sidecar.

        Safe to call for an unregistered sidecar_id (returns False) — this
        runs on every ingest, which must never 404.
        """
        row = session.get(SidecarRegistry, sidecar_id)
        if not row or not row.pending_update:
            return False
        row.pending_update = False
        session.commit()
        logger.info(f"Remote update delivered to sidecar '{scrub_log(sidecar_id)}'")
        return True

    def upsert_sidecar(  # noqa: PLR0913 - one registry row, many reported fields
        self,
        sidecar_id: str,
        source_ip: str,
        session: Session,
        *,
        sidecar_version: str | None = None,
        os_platform: str | None = None,
        self_update_capable: bool | None = None,
        keep_alive: bool | None = None,
        keep_alive_providers: dict[str, bool] | None = None,
        auto_update: bool | None = None,
        update_channel: str | None = None,
        log_level: str | None = None,
        collection_errors: int = 0,
        last_log_lines: list[str] | None = None,
        identity_sources: dict[str, dict[str, str]] | None = None,
    ) -> SidecarRegistry:
        """Insert on first sight; update last_seen and ingest_count on repeat calls."""
        if update_channel not in UPDATE_CHANNELS:
            update_channel = None  # never store a channel the dashboard can't render
        log_level = log_level.upper() if log_level else None
        if log_level not in LOG_LEVELS:
            log_level = None
        row = session.get(SidecarRegistry, sidecar_id)
        if row:
            row.last_seen = datetime.now(UTC)
            row.ingest_count += 1
            row.last_ip = source_ip
            if sidecar_version is not None:
                row.sidecar_version = sidecar_version
            if os_platform is not None:
                row.os_platform = os_platform
            if self_update_capable is not None:
                row.self_update_capable = self_update_capable
            if keep_alive is not None:
                row.keep_alive = keep_alive
            if keep_alive_providers is not None:
                row.keep_alive_providers = json.dumps(parse_provider_flags(keep_alive_providers))
            if auto_update is not None:
                row.auto_update = auto_update
            if update_channel is not None:
                row.update_channel = update_channel
            if log_level is not None:
                row.log_level = log_level
            if collection_errors > 0:
                row.error_count += collection_errors
            if last_log_lines is not None:
                row.recent_logs = _recent_logs_json(last_log_lines)
            if identity_sources is not None:
                row.identity_sources = json.dumps(identity_sources)
            logger.debug(f"Updated sidecar '{sidecar_id}' (ingest #{row.ingest_count})")
        else:
            row = SidecarRegistry(
                sidecar_id=sidecar_id,
                hostname=sidecar_id,
                last_ip=source_ip,
                sidecar_version=sidecar_version,
                os_platform=os_platform,
                self_update_capable=self_update_capable,
                keep_alive=keep_alive,
                keep_alive_providers=(
                    json.dumps(parse_provider_flags(keep_alive_providers))
                    if keep_alive_providers is not None
                    else None
                ),
                auto_update=auto_update,
                update_channel=update_channel,
                log_level=log_level,
                error_count=collection_errors,
                recent_logs=_recent_logs_json(last_log_lines) if last_log_lines else None,
                identity_sources=json.dumps(identity_sources) if identity_sources else None,
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
        # Machine-scoped credential tags and pending origins (#319) belong
        # to this sidecar only — drop them so they don't linger as
        # unresolvable rows (or re-apply if the hostname is ever reused).
        from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

        CredentialTagRepo.delete_for_sidecar(session, sidecar_id=sidecar_id)
        PendingCredentialTagRepo.delete_for_sidecar(session, sidecar_id=sidecar_id)
        # Its discovered credentials go too: a durable ``credential_sources`` row
        # outlives the machine otherwise and keeps reporting a stale health forever.
        for source in session.exec(
            select(CredentialSource).where(CredentialSource.sidecar_id == sidecar_id)
        ).all():
            session.delete(source)
        session.delete(row)
        session.commit()
        logger.info(f"Deleted sidecar from registry: '{scrub_log(sidecar_id)}'")
        return True

    def to_dict(
        self,
        row: SidecarRegistry,
        update_channel: str | None = None,
        auto_update: bool = False,
    ) -> dict:
        """Serialize a SidecarRegistry row to a response dict.

        ``update_channel`` / ``auto_update`` are the fleet-wide values; this sidecar's own
        override (if any) is applied on top, so ``update_available`` is judged against the
        channel the sidecar is actually told to follow."""
        # Imported lazily so unit tests that exercise to_dict don't need
        # the global FastAPI startup wiring.
        from app.services.sidecar_version_checker import (
            is_update_available,
            parse_channel,
            sidecar_version_checker,
        )

        last_seen_utc = row.last_seen.replace(tzinfo=UTC)
        stale = last_seen_utc < datetime.now(UTC) - timedelta(minutes=STALE_THRESHOLD_MINUTES)
        latest_version = sidecar_version_checker.get_latest()
        latest_edge_sha = sidecar_version_checker.get_latest_edge_sha()
        latest_beta = sidecar_version_checker.get_latest_beta()
        # Only offer an update when the build can actually self-update in place.
        # `None` (not reported) stays permissive so already-deployed frozen
        # sidecars keep working; `False` (from-source / Docker) suppresses it.
        # A stale sidecar can't actually receive the push either way — delivery
        # rides its next successful ingest response (see fleet.py:ingest_metrics)
        # — so a dead sidecar would otherwise show "update available" forever
        # even though nothing can apply it. Gate the offer on liveness too.
        # "A newer build exists" is independent of whether it can be pushed
        # right now (#202): an offline sidecar still reports it is behind.
        outdated = is_update_available(
            row.sidecar_version,
            latest_version,
            latest_edge_sha,
            latest_beta,
            target_channel=row.update_channel_desired or update_channel,
        )
        update_available = not stale and row.self_update_capable is not False and outdated
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
            "update_available": update_available,
            "outdated": outdated,
            "self_update_capable": row.self_update_capable,
            "auto_update": row.auto_update,
            "auto_update_desired": row.auto_update_desired,
            "effective_auto_update": effective_auto_update(row, auto_update),
            "log_level": row.log_level,
            "log_level_desired": row.log_level_desired,
            "update_channel": row.update_channel,
            "update_channel_desired": row.update_channel_desired,
            "effective_update_channel": effective_update_channel(row, update_channel),
            "keep_alive": row.keep_alive,
            "keep_alive_desired": row.keep_alive_desired,
            # None = a sidecar that doesn't report per-login state; {} = reports none running.
            "keep_alive_providers": (
                parse_provider_flags(row.keep_alive_providers)
                if row.keep_alive_providers is not None
                else None
            ),
            "keep_alive_desired_providers": parse_provider_flags(row.keep_alive_desired_providers),
            "os_platform": row.os_platform,
            "collection_enabled": row.collection_enabled,
            "pending_update": row.pending_update,
            "stale": stale,
            "stale_threshold_minutes": STALE_THRESHOLD_MINUTES,
            "recent_logs": json.loads(row.recent_logs) if row.recent_logs else [],
            "identity_sources": json.loads(row.identity_sources) if row.identity_sources else {},
        }


fleet_registry = FleetRegistryService()
