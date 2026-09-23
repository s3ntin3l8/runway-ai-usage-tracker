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

from sqlmodel import Session, col, or_, select

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
        providers: list[str],
    ) -> dict[str, dict[str, str]]:
        """Return ``{provider_id: {credential_origin: account_id, ...}}`` for
        every stored tag whose provider is in ``providers``.

        The single source of truth for the ``/fleet/config`` /
        ``/fleet/ingest`` ``account_tag_hints`` shape (PR #288 / #290).
        Production callers — ``_account_tag_hints_for_providers`` in
        ``app/api/endpoints/fleet.py`` — invoke this method so the
        lookup key lives in one place (the repo). Origins without a
        stored tag are silently omitted — the sidecar's silent-listener
        block guard treats missing hints the same way as no hints at
        all (the card stays blocked until the operator resolves it).

        Empty ``providers`` returns an empty map. Empty ``account_id``
        values are filtered out defensively.
        """
        if not providers:
            return {}
        from sqlmodel import or_  # local import keeps the noop-import lint happy

        predicates = [CredentialTag.provider_id == pid for pid in providers]
        rows = list(
            session.exec(
                select(
                    CredentialTag.provider_id,
                    CredentialTag.credential_origin,
                    CredentialTag.account_id,
                ).where(or_(*predicates))
            ).all()
        )
        out: dict[str, dict[str, str]] = {}
        for pid, origin, account_id in rows:
            if isinstance(pid, str) and isinstance(origin, str) and isinstance(account_id, str):
                if pid and origin and account_id:
                    out.setdefault(pid, {})[origin] = account_id
        return out

    @staticmethod
    def auto_hints_for_single_account_providers(
        session: Session,
        *,
        providers: list[str],
    ) -> dict[str, dict[str, str]]:
        """Pre-ship tag-hints for providers with exactly one enabled
        non-default ``provider_configs`` row.

        Closes the MiniMax card-split: when the operator has a single
        labeled MiniMax account (e.g. ``s3ntin318@gmail.com``), the
        sidecar's event stream has nothing to discover upstream and
        would otherwise ship events under the synthetic ``"default"``
        sentinel. Without an auto-hint the events land on a
        standalone synthetic-default card and the quota gauge stays
        orphaned on the labeled row.

        The hint key is ``"provider:<provider_id>"`` (matches
        ``scripts/sidecar.py:credential_origin_for_provider``), and the
        value is the operator's chosen ``account_id``. The hint is
        non-persistent — it lives in the ``/fleet/config`` payload only
        and the operator can still override it via the Untagged
        Credentials dialog if their multi-account setup requires it.

        Skips providers with 0 rows (nothing to retarget), 2+ rows
        (multi-account ambiguity — operator should tag explicitly),
        or where the single row is the ``"default"`` sentinel (the
        sidecar's ``"default"``-tagged events already land on it).

        Empty ``providers`` returns an empty map. Defensively filters
        empty / non-string ``account_id`` values.
        """
        if not providers:
            return {}
        # Group by provider_id, keeping only providers with exactly
        # one enabled non-default row. A GROUP BY + HAVING COUNT(*) = 1
        # would also work, but two queries is clearer here — and the
        # row set is tiny (one entry per configured account).
        from app.models.db import ProviderConfig

        rows = list(
            session.exec(
                select(ProviderConfig).where(
                    or_(*(ProviderConfig.provider_id == pid for pid in providers)),
                    ProviderConfig.enabled == True,  # noqa: E712 — SQLModel needs the ==
                    col(ProviderConfig.account_id) != "default",
                )
            ).all()
        )
        # Bucket by provider_id, skip ambiguous multi-row buckets.
        by_pid: dict[str, list[str]] = {}
        for r in rows:
            by_pid.setdefault(r.provider_id, []).append(r.account_id)
        out: dict[str, dict[str, str]] = {}
        for pid, account_ids in by_pid.items():
            if len(account_ids) != 1:
                continue  # multi-account ambiguity — defer to the operator dialog
            aid = account_ids[0]
            if not isinstance(aid, str) or not aid:
                continue
            # The hint key matches ``scripts/sidecar.py:credential_origin_for_provider``,
            # which is the same descriptor the sidecar reports to
            # /fleet/credentials/manifest when an origin is untagged.
            out.setdefault(pid, {})[f"provider:{pid}"] = aid
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
