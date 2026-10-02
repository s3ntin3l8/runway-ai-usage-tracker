"""Which unmapped credentials are silently stopping a provider's quota collection (#493).

A sidecar that cannot resolve a credential's account withholds the token for providers the
server cannot verify by source, so nothing collects for it until an operator assigns an
account. The Untagged list shows the origin; this says what it is *costing*: the origin is
flagged only when the provider has no fresh collection from any source, so a credential that
merely duplicates a working one stays quiet.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.models.db import CredentialSource, PendingCredentialTag
from app.models.schemas import BlockedCollectionView

# Providers whose bundle the sidecar ships even when identity is unresolved, because the
# server can verify the exact source (``identity_pending`` flow). Mirror of the sidecar's
# ``_SERVER_IDENTITY_PROVIDERS`` (a parity test keeps them equal).
SERVER_IDENTITY_PROVIDERS = frozenset(
    {"antigravity", "anthropic", "chatgpt", "gemini", "github", "opencode"}
)
TOKEN_WITHHELD = "token_withheld"
# A provider counts as collecting if any enabled source succeeded this recently.
FRESH_WINDOW = timedelta(hours=6)


def withholds_token(row: PendingCredentialTag) -> bool:
    """Whether the sidecar kept this origin's token on the machine.

    New sidecars say so (``reason``). An older one sends no reason, but for a provider the
    server cannot verify by source the token is always withheld, so infer it.
    """
    if row.reason is not None:
        return row.reason == TOKEN_WITHHELD
    return row.provider_id not in SERVER_IDENTITY_PROVIDERS


def blocked_collection(
    session: Session, visible_rows: Iterable[PendingCredentialTag], *, now: datetime | None = None
) -> list[BlockedCollectionView]:
    """Unmapped, token-withheld origins whose provider has no fresh collection."""
    rows = [row for row in visible_rows if withholds_token(row)]
    if not rows:
        return []
    cutoff = (now or datetime.now(UTC)) - FRESH_WINDOW
    collecting: set[str] = set()
    for source in session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id.in_({row.provider_id for row in rows})  # type: ignore[attr-defined]
        )
    ).all():
        succeeded = source.last_success_at
        if succeeded is not None and source.enabled:
            if succeeded.tzinfo is None:
                succeeded = succeeded.replace(tzinfo=UTC)
            if succeeded >= cutoff:
                collecting.add(source.provider_id)
    return [
        BlockedCollectionView(
            sidecar_id=row.sidecar_id,
            provider_id=row.provider_id,
            credential_origin=row.credential_origin,
            first_seen=row.first_seen.isoformat() if row.first_seen else None,
        )
        for row in sorted(rows, key=lambda r: (r.provider_id, r.sidecar_id, r.credential_origin))
        if row.provider_id not in collecting
    ]
