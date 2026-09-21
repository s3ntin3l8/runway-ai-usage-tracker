"""Server-side store of operator-resolved credential-origin → account_id mappings.

The silent-listener model: sidecars report ``credential_origin`` values for
credentials they found locally; the operator resolves the unmapped ones in
the fleet UI by selecting a configured ``provider_configs`` row. This repo
owns that resolution table (the ``credential_tags`` SQLModel in
``app/models/db.py``).

It is deliberately a thin object around the SQLModel — no caching layer,
no LRU, no async — because the read path is the ``/fleet/config`` round
trip on the heartbeat cadence (10-min default) and the write path is the
operator's tag dialog, never on a hot path. Any future per-request access
goes through this surface so the lookup key stays in one place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from app.models.db import CredentialTag, PendingCredentialTag


class CredentialTagRepo:
    """Lookup / write / list operations on the ``credential_tags`` table."""

    @staticmethod
    def get(session: Session, *, provider_id: str, credential_origin: str) -> CredentialTag | None:
        """Return the tag for the (provider, origin) pair, or ``None`` if unset."""
        return session.exec(
            select(CredentialTag).where(
                CredentialTag.provider_id == provider_id,
                CredentialTag.credential_origin == credential_origin,
            )
        ).first()

    @staticmethod
    def get_account_id(session: Session, *, provider_id: str, credential_origin: str) -> str | None:
        """Return the resolved ``account_id`` for a (provider, origin) pair, or ``None``.

        Convenience over :meth:`get` for the hot path on the heartbeat
        cadence — endpoints don't need to materialize the full row.
        """
        row = session.exec(
            select(CredentialTag.account_id).where(
                CredentialTag.provider_id == provider_id,
                CredentialTag.credential_origin == credential_origin,
            )
        ).first()
        return row

    @staticmethod
    def set_tag(
        session: Session,
        *,
        provider_id: str,
        credential_origin: str,
        account_id: str,
        set_by: str = "operator",
    ) -> CredentialTag:
        """Set (idempotently) the operator's tag for the (provider, origin) pair.

        If a row already exists for this pair, ``account_id`` and ``set_at``
        are refreshed in-place; otherwise a new row is inserted. Always
        returns the resulting row so callers can echo it back to the UI.
        """
        row = session.exec(
            select(CredentialTag).where(
                CredentialTag.provider_id == provider_id,
                CredentialTag.credential_origin == credential_origin,
            )
        ).first()
        if row is None:
            row = CredentialTag(
                provider_id=provider_id,
                credential_origin=credential_origin,
                account_id=account_id,
                set_by=set_by,
                set_at=datetime.now(UTC),
            )
            session.add(row)
        else:
            row.account_id = account_id
            row.set_by = set_by
            row.set_at = datetime.now(UTC)
        session.flush()
        return row

    @staticmethod
    def delete_tag(session: Session, *, provider_id: str, credential_origin: str) -> bool:
        """Delete a tag if it exists. Returns ``True`` when a row was removed."""
        row = CredentialTagRepo.get(
            session, provider_id=provider_id, credential_origin=credential_origin
        )
        if row is None:
            return False
        session.delete(row)
        session.flush()
        return True

    @staticmethod
    def list_by_provider(session: Session, *, provider_id: str) -> list[CredentialTag]:
        """All tags for one provider, ordered by origin for deterministic UI."""
        return list(
            session.exec(
                select(CredentialTag)
                .where(CredentialTag.provider_id == provider_id)
                .order_by(CredentialTag.credential_origin)
            ).all()
        )

    @staticmethod
    def list_all(session: Session) -> list[CredentialTag]:
        """All tags across providers, ordered for stable serialization."""
        return list(
            session.exec(
                select(CredentialTag).order_by(
                    CredentialTag.provider_id, CredentialTag.credential_origin
                )
            ).all()
        )

    @staticmethod
    def list_pending_payload(
        session: Session,
        *,
        manifests: list[dict[str, Any]],
    ) -> dict[str, dict[str, str]]:
        """Return ``{provider_id: {credential_origin: account_label, ...}}`` for the
        union of ``credential_origins`` across the supplied manifests.

        ``manifests`` shape: ``[{"provider_id": ..., "credential_origins": [...]} ...]``.
        Used by ``/fleet/credentials/manifest`` to enrich its response with the
        hints the sidecar will persist in its next ``/fleet/config`` cycle.

        Origins without a stored tag are silently omitted — the sidecar is
        expected to re-query for unresolved entries via the dedicated
        ``/api/v1/fleet/credentials/tags?credential_origin=...`` endpoint
        the webapp's "Untagged credentials" panel uses.
        """
        pairs = {
            (m["provider_id"], orig) for m in manifests for orig in m.get("credential_origins", [])
        }
        if not pairs:
            return {}
        # Bulk-fetch in one query; SQLModel's IN-translator doesn't compose
        # tuples directly, so we OR over per-pair predicates.
        from sqlmodel import or_  # local import keeps the noop-import lint happy

        predicates = [
            (CredentialTag.provider_id == pid) & (CredentialTag.credential_origin == orig)
            for pid, orig in pairs
        ]
        rows = list(session.exec(select(CredentialTag).where(or_(*predicates))).all())
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            out.setdefault(row.provider_id, {})[row.credential_origin] = row.account_id
        return out


class PendingCredentialTagRepo:
    """Read/write operations on the ``pending_credential_tags`` table.

    Maintained in lockstep with the sidecar's manifest reporting cycle.
    The single-statement operations are kept thin because the manifest
    endpoint orchestrates them in a single transaction (upsert each entry,
    then delete any pending row for the sidecar that's not in the new
    list).
    """

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
    def delete_stale(
        session: Session,
        *,
        sidecar_id: str,
        keep_origins_by_provider: dict[str, set[str]],
    ) -> int:
        """Delete pending rows for ``sidecar_id`` whose (provider, origin) is not
        in the supplied ``keep_origins_by_provider`` map.

        Returns the number of rows removed. Called by the manifest endpoint
        after upserting the cycle's entries: ``keep_origins_by_provider`` is
        ``{provider_id: {credential_origin, ...}}`` derived from the
        sidecar's manifest body. A pair absent from the sidecar's manifest
        means the sidecar's local state has dropped that credential
        (de-installed, re-configured, or the sidecar crashed mid-discovery)
        — clearing it keeps the fleet UI's "Untagged" panel truthful.

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
    def pending_count_by_sidecar(session: Session) -> dict[str, int]:
        """Aggregate pending counts per sidecar_id. Powers the fleet banner total."""
        from sqlmodel import func

        rows = list(
            session.exec(
                select(
                    PendingCredentialTag.sidecar_id,
                    func.count(),
                ).group_by(PendingCredentialTag.sidecar_id)
            ).all()
        )
        return {sidecar_id: int(count) for sidecar_id, count in rows}
