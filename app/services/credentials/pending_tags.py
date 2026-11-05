from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app.models.db import PendingCredentialTag
from app.services.credentials.tags import CredentialTagRepo, safe_quota_preview

VERIFY_BACKOFF_BASE = timedelta(minutes=15)
VERIFY_BACKOFF_MAX = timedelta(hours=6)


def retry_backoff(attempts: int) -> timedelta:
    """Delay before the next try: 15 min doubling, capped at 6 h (shared by every retry schedule)."""
    return min(
        VERIFY_BACKOFF_BASE * 2 ** min(max(attempts - 1, 0), 10),
        VERIFY_BACKOFF_MAX,
    )


class PendingCredentialTagRepo:
    """Read/write operations on the ``pending_credential_tags`` table.

    Maintained in lockstep with the sidecar's manifest reporting cycle.
    The single-statement operations are kept thin because the manifest
    endpoint orchestrates them in a single transaction (upsert each entry,
    then delete any pending row for the sidecar that's not in the new
    list).
    """

    @staticmethod
    def set_quota_preview(
        session: Session,
        *,
        sidecar_id: str,
        provider_id: str,
        credential_origin: str,
        preview: list[dict[str, Any]],
        observed_at: datetime | None = None,
    ) -> PendingCredentialTag | None:
        """Persist a whitelisted quota-only preview for a pending source.

        If the source is already tagged, its pending row and any saved preview
        are deleted; the preview is intentionally discarded because the source
        is now identified.
        """
        if (
            CredentialTagRepo.get_account_id(
                session,
                provider_id=provider_id,
                credential_origin=credential_origin,
                sidecar_id=sidecar_id,
            )
            is not None
        ):
            PendingCredentialTagRepo.delete(
                session,
                sidecar_id=sidecar_id,
                provider_id=provider_id,
                credential_origin=credential_origin,
            )
            return None

        row = PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=sidecar_id,
            provider_id=provider_id,
            credential_origin=credential_origin,
        )
        safe_preview = safe_quota_preview(preview)
        row.quota_preview_json = json.dumps(safe_preview)
        row.quota_preview_observed_at = observed_at or datetime.now(UTC)
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def read_quota_preview(
        row: PendingCredentialTag,
        *,
        now: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Return preview data, observation time, and staleness.

        Expired values are hidden immediately while their observation time is
        retained so the UI can explain why no preview is shown. A maintenance
        task clears their stored payloads.
        """
        observed_at = row.quota_preview_observed_at
        if observed_at is None:
            return [], None, False
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        observed_at_text = observed_at.isoformat()

        from app.core.config import settings

        current = now or datetime.now(UTC)
        if current - observed_at > timedelta(
            seconds=settings.PENDING_CREDENTIAL_PREVIEW_MAX_AGE_SECONDS
        ):
            return [], observed_at_text, True

        try:
            payload = json.loads(row.quota_preview_json or "[]")
        except (TypeError, json.JSONDecodeError):
            payload = []
        preview = safe_quota_preview(
            [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, list)
            else []
        )
        return preview, observed_at_text, False

    @staticmethod
    def expire_quota_previews(session: Session, *, now: datetime | None = None) -> int:
        """Clear aged preview payloads and return the number cleared."""
        from app.core.config import settings

        current = now or datetime.now(UTC)
        ttl = timedelta(seconds=settings.PENDING_CREDENTIAL_PREVIEW_MAX_AGE_SECONDS)
        rows = session.exec(select(PendingCredentialTag)).all()
        cleared = 0
        for row in rows:
            had_preview = row.quota_preview_json is not None
            observed_at = row.quota_preview_observed_at
            if observed_at is not None and observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            stale = observed_at is not None and current - observed_at > ttl
            if stale and had_preview:
                row.quota_preview_json = None
                session.add(row)
                cleared += 1
        if cleared:
            session.flush()
        return cleared

    @staticmethod
    def upsert(
        session: Session,
        *,
        sidecar_id: str,
        provider_id: str,
        credential_origin: str,
    ) -> PendingCredentialTag:
        """Upsert a pending row, refreshing ``last_seen`` if it already exists."""
        row = session.exec(
            select(PendingCredentialTag).where(
                PendingCredentialTag.sidecar_id == sidecar_id,
                PendingCredentialTag.provider_id == provider_id,
                PendingCredentialTag.credential_origin == credential_origin,
            )
        ).first()
        now = datetime.now(UTC)
        if row is None:
            row = PendingCredentialTag(
                sidecar_id=sidecar_id,
                provider_id=provider_id,
                credential_origin=credential_origin,
                first_seen=now,
                last_seen=now,
            )
            session.add(row)
        else:
            row.last_seen = now
        session.flush()
        return row

    @staticmethod
    def verification_backoff(attempts: int) -> timedelta:
        """Delay before the next verification try: 15 min doubling, capped at 6 h."""
        return retry_backoff(attempts)

    @staticmethod
    def get_verify_schedule(
        session: Session, *, provider_id: str
    ) -> dict[tuple[str, str], datetime | None]:
        """``{(sidecar_id, origin): next_verify_at}`` for a provider's pending rows."""
        rows = session.exec(
            select(PendingCredentialTag).where(PendingCredentialTag.provider_id == provider_id)
        ).all()
        out: dict[tuple[str, str], datetime | None] = {}
        for row in rows:
            at = row.next_verify_at
            if at is not None and at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            out[(row.sidecar_id, row.credential_origin)] = at
        return out

    @staticmethod
    def record_verify_attempt(
        session: Session,
        *,
        sidecar_id: str,
        provider_id: str,
        credential_origin: str,
        now: datetime | None = None,
    ) -> None:
        """Count an unsuccessful verification try and schedule the next one."""
        if (
            CredentialTagRepo.get_account_id(
                session,
                provider_id=provider_id,
                credential_origin=credential_origin,
                sidecar_id=sidecar_id,
            )
            is not None
        ):
            return  # identified (or operator-tagged) meanwhile: nothing left to verify
        # Upsert: a pending source the manifest hasn't (or no longer) listed must still earn
        # a backoff, or it would sit first in line every cycle and starve the others.
        row = PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=sidecar_id,
            provider_id=provider_id,
            credential_origin=credential_origin,
        )
        row.verify_attempts = (row.verify_attempts or 0) + 1
        row.next_verify_at = (now or datetime.now(UTC)) + (
            PendingCredentialTagRepo.verification_backoff(row.verify_attempts)
        )
        session.add(row)
        session.flush()

    @staticmethod
    def reset_verification(
        session: Session, *, sidecar_id: str, provider_id: str, credential_origin: str
    ) -> None:
        """Retry on the next cycle: the source holds a different secret now."""
        row = session.exec(
            select(PendingCredentialTag).where(
                PendingCredentialTag.sidecar_id == sidecar_id,
                PendingCredentialTag.provider_id == provider_id,
                PendingCredentialTag.credential_origin == credential_origin,
            )
        ).first()
        if row is None or (not row.verify_attempts and row.next_verify_at is None):
            return
        row.verify_attempts = 0
        row.next_verify_at = None
        session.add(row)
        session.flush()

    @staticmethod
    def delete_stale(
        session: Session,
        *,
        sidecar_id: str,
        keep_origins_by_provider: dict[str, set[str]],
    ) -> int:
        """Delete pending rows for ``sidecar_id`` whose (provider, origin) is not
        in the supplied ``keep_origins_by_provider`` map.

        Returns the number of rows removed. Called by the manifest endpoint
        only for providers the sidecar says completed cleanly:
        ``keep_origins_by_provider`` is
        ``{provider_id: {credential_origin, ...}}`` from those scans. A pair
        absent from a completed provider scan means the sidecar no longer
        found that credential; providers omitted after an error are untouched.

        NB: a transient discovery failure could delete a row that the
        sidecar re-reports next cycle, re-promoting the same origin back
        to "needs attention". That bounce is acceptable — operators
        seeing the same entry flash twice will diagnose the sidecar's
        credential-discovery flake, not the server.
        """
        rows = list(
            session.exec(
                select(PendingCredentialTag).where(
                    PendingCredentialTag.sidecar_id == sidecar_id,
                )
            ).all()
        )
        removed = 0
        for row in rows:
            kept = keep_origins_by_provider.get(row.provider_id, set())
            if row.credential_origin not in kept:
                session.delete(row)
                removed += 1
        session.flush()
        return removed

    @staticmethod
    def list_all(
        session: Session,
        *,
        sidecar_id: str | None = None,
    ) -> list[PendingCredentialTag]:
        """All pending entries, optionally filtered by sidecar. Sorted for stable UI."""
        stmt = select(PendingCredentialTag)
        if sidecar_id is not None:
            stmt = stmt.where(PendingCredentialTag.sidecar_id == sidecar_id)
        stmt = stmt.order_by(
            PendingCredentialTag.sidecar_id,
            PendingCredentialTag.provider_id,
            PendingCredentialTag.credential_origin,
        )
        return list(session.exec(stmt).all())

    @staticmethod
    def get(
        session: Session,
        *,
        sidecar_id: str,
        provider_id: str,
        credential_origin: str,
    ) -> PendingCredentialTag | None:
        """Look up a single pending entry by its three-component identity."""
        return session.exec(
            select(PendingCredentialTag).where(
                PendingCredentialTag.sidecar_id == sidecar_id,
                PendingCredentialTag.provider_id == provider_id,
                PendingCredentialTag.credential_origin == credential_origin,
            )
        ).first()

    @staticmethod
    def delete(
        session: Session,
        *,
        sidecar_id: str,
        provider_id: str,
        credential_origin: str,
    ) -> bool:
        """Delete a pending entry. Returns ``True`` when a row was removed."""
        row = PendingCredentialTagRepo.get(
            session,
            sidecar_id=sidecar_id,
            provider_id=provider_id,
            credential_origin=credential_origin,
        )
        if row is None:
            return False
        session.delete(row)
        session.flush()
        return True

    @staticmethod
    def delete_for_sidecar(session: Session, *, sidecar_id: str) -> int:
        """Drop every pending row reported by ``sidecar_id``."""
        rows = list(
            session.exec(
                select(PendingCredentialTag).where(PendingCredentialTag.sidecar_id == sidecar_id)
            ).all()
        )
        for row in rows:
            session.delete(row)
        if rows:
            session.flush()
        return len(rows)

    @staticmethod
    def delete_by_origin(
        session: Session,
        *,
        provider_id: str,
        credential_origin: str,
    ) -> int:
        """Delete every sidecar's pending row for a (provider, origin) pair.

        Used when the operator tags the origin deployment-wide
        (``scope="deployment"``, #319): the mapping now resolves for
        every host, so no sidecar should keep re-prompting. Returns the
        number of rows removed.
        """
        rows = list(
            session.exec(
                select(PendingCredentialTag).where(
                    PendingCredentialTag.provider_id == provider_id,
                    PendingCredentialTag.credential_origin == credential_origin,
                )
            ).all()
        )
        for row in rows:
            session.delete(row)
        if rows:
            session.flush()
        return len(rows)
