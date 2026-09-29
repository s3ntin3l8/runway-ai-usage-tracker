"""Shared query used by any Data Health check that needs to offer a
"reassign to a real account" fix: the provider's other configured,
non-archived, non-`default` accounts. Server-revalidated on every
`plan`/`apply` call — never trust a `target` param without checking it
against this list again, since it may be stale by the time the request
lands.
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from app.models.db import LatestUsage, ProviderConfig


def candidate_targets(session: Session, provider_id: str) -> list[str]:
    rows = session.exec(
        select(ProviderConfig.account_id).where(
            col(ProviderConfig.provider_id) == provider_id,
            col(ProviderConfig.account_id) != "default",
            col(ProviderConfig.archived).is_(False),
        )
    ).all()
    discovered = session.exec(
        select(LatestUsage.account_id).where(
            LatestUsage.provider_id == provider_id,
            col(LatestUsage.account_id) != "default",
        )
    ).all()
    archived = set(
        session.exec(
            select(ProviderConfig.account_id).where(
                ProviderConfig.provider_id == provider_id,
                col(ProviderConfig.archived).is_(True),
            )
        ).all()
    )
    return sorted((set(rows) | set(discovered)) - archived)
