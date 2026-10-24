"""Which credential sources a sidecar found but could not read (#711).

A browser cookie store can be unusable: the browser holds it open (a Win32 sharing
violation on Chrome 127+), or its values are app-bound encrypted and need a browser-side
read. The sidecar classifies the store once per cycle and reports it to the server, so a
provider whose credential sits behind such a store is not mistaken for a provider nobody
is signed into.

The rows live on ``pending_credential_tags`` under a ``source_*`` reason: that table is
where "the sidecar reported this origin and here is why" already lives, and its per-cycle
prune gives the report an automatic lifecycle - it disappears the cycle the store becomes
readable again. These rows are kept out of the "Untagged credentials" surface, because
there is no credential to map; they are surfaced here instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlmodel import Session, select

from app.models.db import PendingCredentialTag
from app.models.schemas import BlockedSourceView
from app.services.blocked_collection import collecting_providers

# Why the sidecar reported an origin it could not read. Mirror of the sidecar's
# ``SOURCE_BLOCK_REASONS`` (a parity test keeps them equal).
SOURCE_REASONS = frozenset({"source_locked", "source_unsupported", "source_unreadable"})


def is_source_blocked(row: PendingCredentialTag) -> bool:
    """Whether this row records a credential source that could not be read."""
    return row.reason in SOURCE_REASONS


def source_block_rows(session: Session) -> list[PendingCredentialTag]:
    """Every row a sidecar reported as an unusable credential source."""
    return list(
        session.exec(
            select(PendingCredentialTag).where(
                PendingCredentialTag.reason.in_(sorted(SOURCE_REASONS))  # type: ignore[union-attr]
            )
        ).all()
    )


def source_blocks(
    session: Session, rows: Iterable[PendingCredentialTag], *, now: datetime | None = None
) -> list[BlockedSourceView]:
    """The unusable credential sources, each flagged with whether its provider collects.

    A locked store is still worth knowing about when another source (an environment
    variable, a different browser) covers the provider, so every row is returned; the
    ``collecting`` flag lets the caller tell the two apart.
    """
    blocked = [row for row in rows if is_source_blocked(row)]
    if not blocked:
        return []
    collecting = collecting_providers(session, {row.provider_id for row in blocked}, now=now)
    return [
        BlockedSourceView(
            sidecar_id=row.sidecar_id,
            provider_id=row.provider_id,
            credential_origin=row.credential_origin,
            source_store=row.source_store,
            reason=row.reason or "",
            detail=row.source_detail,
            remedy=row.source_remedy,
            collecting=row.provider_id in collecting,
            first_seen=row.first_seen.isoformat() if row.first_seen else None,
            last_seen=row.last_seen.isoformat() if row.last_seen else None,
        )
        for row in sorted(blocked, key=lambda r: (r.provider_id, r.sidecar_id, r.credential_origin))
    ]
