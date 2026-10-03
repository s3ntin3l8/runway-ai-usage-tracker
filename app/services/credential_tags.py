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

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, and_, col, or_, select

from app.models.db import CredentialTag, PendingCredentialTag

_QUOTA_PREVIEW_FIELDS = frozenset(
    {
        "service_name",
        "remaining",
        "unit",
        "unit_type",
        "pct_used",
        "window_type",
        "reset",
        "reset_at",
    }
)


def safe_quota_preview(preview: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the quota fields safe to persist and return to the UI."""
    return [{key: item[key] for key in _QUOTA_PREVIEW_FIELDS if key in item} for item in preview]


def live_sidecar_ids(session: Session) -> list[str]:
    """Sidecar ids whose ``last_seen`` is within the last 7 days.

    ``sidecar_registry`` rows are never pruned automatically, so a raw
    row count would let one retired machine distort multi-host
    detection forever. Window: comfortably beyond the ~60s heartbeat
    cadence and the 60-min staleness threshold, short enough that
    retired machines age out (PR #318 round-2 re-review S).
    """
    from datetime import timedelta

    from app.models.db import SidecarRegistry

    cutoff = datetime.now(UTC) - timedelta(days=7)
    return list(
        session.exec(
            select(SidecarRegistry.sidecar_id).where(  # type: ignore[arg-type]
                SidecarRegistry.last_seen >= cutoff
            )
        ).all()
    )


def origin_candidates(credential_origin: str) -> list[str]:
    """The origins whose tags apply to *credential_origin*, most specific first.

    A key-scoped origin (``env:ZAI_API_KEY#<fp>``) is also covered by a tag written
    against its plain origin (``env:ZAI_API_KEY``): tags made before the origin was
    fingerprinted must keep applying, exactly as the sidecar's own hint lookup does.
    """
    from app.services.account_identity import split_keyed_origin

    base, fingerprint = split_keyed_origin(credential_origin)
    return [credential_origin, base] if fingerprint else [credential_origin]


def pick_effective_tag(
    candidates: list[CredentialTag], sidecar_id: str | None
) -> CredentialTag | None:
    """The tag that applies on *sidecar_id* among rows for one origin: the machine-scoped
    one if present, else the deployment-wide one. In-memory twin of the ordering
    :meth:`CredentialTagRepo.get` asks the database for, kept next to it so the two stay
    in step."""
    for tag in candidates:
        if tag.sidecar_id and tag.sidecar_id == sidecar_id:
            return tag
    return next((tag for tag in candidates if tag.sidecar_id is None), None)


class CredentialTagRepo:
    """Lookup / write / list operations on the ``credential_tags`` table."""

    @staticmethod
    def provider_origin(provider_id: str) -> str:
        """Return the origin key used when a provider has no event identity."""
        return f"provider:{provider_id}"

    @staticmethod
    def get(
        session: Session,
        *,
        provider_id: str,
        credential_origin: str,
        sidecar_id: str | None = None,
    ) -> CredentialTag | None:
        """Return the effective tag for the (provider, origin) pair, or ``None``.

        With ``sidecar_id`` given: the sidecar-scoped row wins over a
        deployment-wide (NULL) row for the same origin. With
        ``sidecar_id=None``: only deployment-wide rows match (the view
        an unidentified requester gets).
        """
        for origin in origin_candidates(credential_origin):
            stmt = select(CredentialTag).where(
                CredentialTag.provider_id == provider_id,
                CredentialTag.credential_origin == origin,
            )
            if sidecar_id is None:
                stmt = stmt.where(col(CredentialTag.sidecar_id).is_(None))
            else:
                stmt = (
                    stmt.where(
                        or_(
                            CredentialTag.sidecar_id == sidecar_id,
                            col(CredentialTag.sidecar_id).is_(None),
                        )
                    )
                    # Scoped rows (False) sort before deployment-wide (True).
                    .order_by(col(CredentialTag.sidecar_id).is_(None))
                )
            found = session.exec(stmt).first()
            if found is not None:
                return found
        return None

    @staticmethod
    def get_redirect(
        session: Session, *, provider_id: str, sidecar_id: str | None = None
    ) -> CredentialTag | None:
        """Return the provider-level tag when it redirects to another provider.

        ``target_provider_id`` is written only by :meth:`set_tag` (NULL = the tag's
        own provider, and any re-tag resets it).
        """
        tag = CredentialTagRepo.get(
            session,
            provider_id=provider_id,
            credential_origin=CredentialTagRepo.provider_origin(provider_id),
            sidecar_id=sidecar_id,
        )
        if tag is not None and tag.target_provider_id and tag.target_provider_id != provider_id:
            return tag
        return None

    @staticmethod
    def get_account_id(
        session: Session,
        *,
        provider_id: str,
        credential_origin: str,
        sidecar_id: str | None = None,
    ) -> str | None:
        """Return the effective ``account_id`` for a (provider, origin) pair.

        Convenience over :meth:`get` for the hot path on the heartbeat
        cadence — endpoints don't need to materialize the full row.
        Scoped-first precedence matches :meth:`get`.
        """
        tag = CredentialTagRepo.get(
            session,
            provider_id=provider_id,
            credential_origin=credential_origin,
            sidecar_id=sidecar_id,
        )
        return tag.account_id if tag else None

    @staticmethod
    def set_tag(
        session: Session,
        *,
        provider_id: str,
        credential_origin: str,
        account_id: str,
        sidecar_id: str | None = None,
        set_by: str = "operator",
        target_provider_id: str | None = None,
    ) -> CredentialTag:
        """Set (idempotently) the operator's tag for the (provider, origin) pair.

        ``sidecar_id=None`` writes a deployment-wide (NULL) row — the
        dialog's "All machines" scope and every pre-#319 legacy row. A
        concrete id writes that sidecar's own row (the dialog default,
        "This machine"). If a row already exists for the same scope,
        ``account_id`` and ``set_at`` are refreshed in-place; otherwise a
        new row is inserted. Always returns the resulting row so callers
        can echo it back to the UI.

        ``target_provider_id`` is always written (including ``None``), so any
        re-tag clears a previous cross-provider redirect.
        """
        stmt = select(CredentialTag).where(
            CredentialTag.provider_id == provider_id,
            CredentialTag.credential_origin == credential_origin,
        )
        if sidecar_id is None:
            stmt = stmt.where(col(CredentialTag.sidecar_id).is_(None))
        else:
            stmt = stmt.where(CredentialTag.sidecar_id == sidecar_id)
        row = session.exec(stmt).first()
        if row is None:
            row = CredentialTag(
                provider_id=provider_id,
                credential_origin=credential_origin,
                account_id=account_id,
                sidecar_id=sidecar_id,
                set_by=set_by,
                set_at=datetime.now(UTC),
                target_provider_id=target_provider_id,
            )
            session.add(row)
        else:
            row.account_id = account_id
            row.target_provider_id = target_provider_id
            row.set_by = set_by
            row.set_at = datetime.now(UTC)
        session.flush()
        return row

    @staticmethod
    def inherited_account_for_rotation(
        session: Session,
        *,
        provider_id: str,
        credential_origin: str,
        sidecar_id: str | None,
        claimed_account_id: str | None = None,
    ) -> str | None:
        """The account a freshly rotated origin should be carried over to, if any.

        A CLI re-login rewrites the credential, which re-fingerprints the
        origin (#349): the operator's tag is keyed to the old fingerprint, so
        the source drops back to Untagged and its token is withheld until
        someone re-tags it — a silent quota outage (#474). When the *base*
        origin (everything before ``#<fingerprint>``) is unchanged and every
        in-scope tag written against that base agrees on one account, the
        binding is unambiguous and this returns that account for the new
        origin to inherit.

        Conservative by construction:

        * only fingerprinted origins — ``split_keyed_origin`` yields
          ``fingerprint=None`` for a plain origin, so a path-only tag can never
          hop to a different machine's credential;
        * only tags scoped to ``sidecar_id`` or deployment-wide (NULL): another
          machine's tag is not evidence about this one (same precedence as
          :meth:`get`);
        * exactly one distinct account across those rows — two accounts ever
          tagged at this location is an operator decision, not an inheritance;
        * a contradicting ``claimed_account_id`` (the credential's own reported
          identity) vetoes inheritance: the account changed under the path.

        Identity-claim rows count as evidence (they are a binding someone
        allowed), so a stale claim that disagrees with an operator tag makes
        this refuse rather than guess.

        Provenance is one-shot: the row written here reads ``set_by=rotation``
        only until the operator re-saves it in the tag dialog, which replaces
        it with their own actor — after that it is indistinguishable from a
        mapping they made by hand (only ``set_at`` is history).
        """
        from app.services.account_identity import canonical_account_id, split_keyed_origin

        base, fingerprint = split_keyed_origin(credential_origin)
        if fingerprint is None:
            return None
        prefix = f"{base}#"
        stmt = select(CredentialTag.credential_origin, CredentialTag.account_id).where(
            CredentialTag.provider_id == provider_id,
        )
        if sidecar_id is None:
            stmt = stmt.where(col(CredentialTag.sidecar_id).is_(None))
        else:
            stmt = stmt.where(
                or_(
                    CredentialTag.sidecar_id == sidecar_id,
                    col(CredentialTag.sidecar_id).is_(None),
                )
            )
        # Python prefix match, not SQL LIKE: a path may contain "_" or "%",
        # and LIKE would let one location's tag answer for a look-alike one.
        accounts = {
            account
            for origin, account in session.exec(stmt).all()
            if isinstance(origin, str)
            and isinstance(account, str)
            and account
            and origin.startswith(prefix)
        }
        if len(accounts) != 1:
            return None
        account = next(iter(accounts))
        if isinstance(claimed_account_id, str) and claimed_account_id.strip():
            if canonical_account_id(claimed_account_id) != canonical_account_id(account):
                return None
        return account

    @staticmethod
    def delete_tag(
        session: Session,
        *,
        provider_id: str,
        credential_origin: str,
        sidecar_id: str | None = None,
    ) -> bool:
        """Delete tag(s) for the (provider, origin) pair.

        ``sidecar_id=None`` removes every row for the pair (any scope) —
        the "forget this origin entirely" cleanup. A concrete id removes
        only that sidecar's row. Returns ``True`` when at least one row
        was removed.
        """
        stmt = select(CredentialTag).where(
            CredentialTag.provider_id == provider_id,
            CredentialTag.credential_origin == credential_origin,
        )
        if sidecar_id is not None:
            stmt = stmt.where(CredentialTag.sidecar_id == sidecar_id)
        rows = list(session.exec(stmt).all())
        for row in rows:
            session.delete(row)
        if rows:
            session.flush()
        return bool(rows)

    @staticmethod
    def delete_tag_in_scope(
        session: Session,
        *,
        provider_id: str,
        credential_origin: str,
        sidecar_id: str | None,
    ) -> bool:
        """Delete exactly one scope's tag for the (provider, origin) pair.

        Unlike :meth:`delete_tag` (where ``sidecar_id=None`` means "every
        scope"), ``None`` here targets only the deployment-wide row — the
        operator removes the one mapping they are looking at.
        """
        stmt = select(CredentialTag).where(
            CredentialTag.provider_id == provider_id,
            CredentialTag.credential_origin == credential_origin,
        )
        if sidecar_id is None:
            stmt = stmt.where(col(CredentialTag.sidecar_id).is_(None))
        else:
            stmt = stmt.where(CredentialTag.sidecar_id == sidecar_id)
        rows = list(session.exec(stmt).all())
        for row in rows:
            session.delete(row)
        if rows:
            session.flush()
        return bool(rows)

    @staticmethod
    def delete_scoped_for_origin(
        session: Session,
        *,
        provider_id: str,
        credential_origin: str,
    ) -> int:
        """Drop every machine-scoped tag for the (provider, origin) pair.

        Used when the operator tags the origin for "All machines": scoped
        rows resolve before the deployment-wide one, so leaving them would
        keep those machines on the old mapping with no way to see why.
        """
        rows = list(
            session.exec(
                select(CredentialTag).where(
                    CredentialTag.provider_id == provider_id,
                    CredentialTag.credential_origin == credential_origin,
                    col(CredentialTag.sidecar_id).is_not(None),
                )
            ).all()
        )
        for row in rows:
            session.delete(row)
        if rows:
            session.flush()
        return len(rows)

    @staticmethod
    def delete_for_sidecar(session: Session, *, sidecar_id: str) -> int:
        """Drop every tag scoped to ``sidecar_id`` (sidecar removed from the fleet)."""
        rows = list(
            session.exec(select(CredentialTag).where(CredentialTag.sidecar_id == sidecar_id)).all()
        )
        for row in rows:
            session.delete(row)
        if rows:
            session.flush()
        return len(rows)

    @staticmethod
    def delete_by_account(session: Session, *, provider_id: str, account_id: str) -> int:
        """Drop every tag whose ``(provider_id, account_id)`` matches.

        Called when the operator removes a ``provider_configs`` row so the
        sidecar stops receiving ``origin -> account_id`` hints for the
        just-deleted account (otherwise the next heartbeat re-asserts
        the identity via ``list_pending_payload`` and keeps stamping
        cards with it — see PR #317 review warning). Unique key on the
        table is ``(provider_id, credential_origin)`` so multiple rows
        can share the same ``account_id``; this removes all of them.

        ``provider_id`` is the *effective* provider: a redirect tag whose
        ``target_provider_id`` matches is removed, while one that merely
        shares the account id under its own provider is kept.

        Returns the number of rows actually removed (0 if none matched).
        """
        rows = list(
            session.exec(
                select(CredentialTag).where(
                    CredentialTag.account_id == account_id,
                    or_(
                        CredentialTag.target_provider_id == provider_id,
                        and_(
                            CredentialTag.provider_id == provider_id,
                            col(CredentialTag.target_provider_id).is_(None),
                        ),
                    ),
                )
            ).all()
        )
        for row in rows:
            session.delete(row)
        if rows:
            session.flush()
        return len(rows)

    @staticmethod
    def list_for_account_provider(session: Session, *, provider_id: str) -> list[CredentialTag]:
        """Tags whose account belongs to ``provider_id`` (effective provider)."""
        return list(
            session.exec(
                select(CredentialTag)
                .where(
                    or_(
                        CredentialTag.target_provider_id == provider_id,
                        and_(
                            CredentialTag.provider_id == provider_id,
                            col(CredentialTag.target_provider_id).is_(None),
                        ),
                    )
                )
                .order_by(CredentialTag.credential_origin)
            ).all()
        )

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
        sidecar_id: str | None = None,
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

        Scoping (#319): with ``sidecar_id`` given, both that sidecar's
        own tags and deployment-wide (NULL) rows are returned, scoped
        winning on conflict; with ``sidecar_id=None`` (an unidentified
        requester), only deployment-wide rows ship.

        Empty ``providers`` returns an empty map. Empty ``account_id``
        values are filtered out defensively.
        """
        if not providers:
            return {}

        predicates = [CredentialTag.provider_id == pid for pid in providers]
        stmt = select(
            CredentialTag.provider_id,
            CredentialTag.credential_origin,
            CredentialTag.account_id,
            CredentialTag.sidecar_id,
        ).where(or_(*predicates), CredentialTag.set_by != "identity_claim")
        if sidecar_id is None:
            stmt = stmt.where(col(CredentialTag.sidecar_id).is_(None))
        else:
            stmt = stmt.where(
                or_(
                    CredentialTag.sidecar_id == sidecar_id,
                    col(CredentialTag.sidecar_id).is_(None),
                )
            )
        rows = list(session.exec(stmt).all())
        out: dict[str, dict[str, str]] = {}
        for pid, origin, account_id, row_sidecar_id in rows:
            if isinstance(pid, str) and isinstance(origin, str) and isinstance(account_id, str):
                if pid and origin and account_id:
                    bucket = out.setdefault(pid, {})
                    if row_sidecar_id is None:
                        # Deployment-wide row — never clobbers a scoped one.
                        bucket.setdefault(origin, account_id)
                    else:
                        # Sidecar-scoped row wins over deployment-wide.
                        bucket[origin] = account_id
        return out

    @staticmethod
    def auto_hints_for_single_account_providers(
        session: Session,
        *,
        providers: list[str],
        sidecar_id: str | None = None,
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

        **Per-sidecar scoping (#319, replaces the PR #318 suppression
        gate):** ``provider_configs`` is deployment-wide, so the
        candidate hint would cross-contaminate hosts in a multi-host
        deployment (the Hermes warning #2 case — a second host whose
        local discovery returns ``default``/``None`` adopting the
        single account's identity). Delivery is now scoped to the
        requesting sidecar:

        - ≤1 live sidecar (7-day ``last_seen`` window — retired
          machines age out): ship unconditionally. Preserves the
          first-cycle MiniMax fix for single-host deployments and
          keeps working for config fetches from old sidecar binaries
          that don't identify themselves.
        - 2+ live sidecars + identified requester: ship only when the
          requester has a ``pending_credential_tags`` row for
          ``provider:<pid>`` — i.e. that host actually reported the
          credential. A host that never reported it never receives the
          hint (the cross-host attribution block).
        - 2+ live sidecars + unidentified requester (old binary): ship
          nothing — the safe pre-#319 behavior for multi-host.

        Empty ``providers`` returns an empty map. Defensively filters
        empty / non-string ``account_id`` values.
        """
        if not providers:
            return {}

        live_ids = live_sidecar_ids(session)
        multi_host = len(live_ids) > 1
        if multi_host and sidecar_id is None:
            import logging

            logging.debug(
                "auto-hint withheld: %d live sidecar(s) and requester "
                "did not identify itself on /fleet/config (pre-#319 "
                "sidecar binary?)",
                len(live_ids),
            )
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
            if multi_host:
                # The requester must have reported this provider's
                # synthetic origin (blocked card / untagged events)
                # before the deployment-wide single-account identity
                # is allowed to reach it.
                reported = session.exec(
                    select(PendingCredentialTag.id).where(
                        PendingCredentialTag.sidecar_id == sidecar_id,  # type: ignore[arg-type]
                        PendingCredentialTag.provider_id == pid,
                        PendingCredentialTag.credential_origin == f"provider:{pid}",
                    )
                ).first()
                if reported is None:
                    continue
            # The hint key matches ``scripts/sidecar.py:credential_origin_for_provider``,
            # which is the same descriptor the sidecar reports to
            # /fleet/credentials/manifest when an origin is untagged.
            out.setdefault(pid, {})[f"provider:{pid}"] = aid
        return out


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
