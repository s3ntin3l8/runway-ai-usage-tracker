"""Durable account merges: where a retired account id went.

The account merge moves an account's history, credential sources and tags to
the destination and retires the source id. The machine, however, keeps
resolving its credential to the source id and pushes it every cycle — so
without a record of the move the source account comes back, with a live
credential, and the destination is left with a credential-source row it
cannot collect with.

This module holds that record and the one lookup the ingest needs.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models.db import ProviderAccountMerge
from app.services.account_identity import canonical_account_id


def record_account_merge(
    session: Session, provider_id: str, source_account_id: str, destination_account_id: str
) -> None:
    """Record that the source account went to the destination.

    One row per (provider, source account): merging the same source again
    replaces the earlier record — the latest merge wins.
    """
    source = canonical_account_id(source_account_id)
    destination = canonical_account_id(destination_account_id)
    row = session.exec(
        select(ProviderAccountMerge).where(
            ProviderAccountMerge.provider_id == provider_id,
            ProviderAccountMerge.source_account_id == source,
        )
    ).first()
    if row is None:
        session.add(
            ProviderAccountMerge(
                provider_id=provider_id,
                source_account_id=source,
                destination_account_id=destination,
            )
        )
    else:
        row.destination_account_id = destination
    session.flush()


def resolve_merged_account(session: Session, provider_id: str, account_id: str) -> str:
    """The account a merged-away id now belongs to, or the id itself.

    Follows the chain (alice → bob → carol) and stops on a cycle, so a
    malformed record cannot hang the ingest.
    """
    current = canonical_account_id(account_id)
    seen = {current}
    while True:
        row = session.exec(
            select(ProviderAccountMerge).where(
                ProviderAccountMerge.provider_id == provider_id,
                ProviderAccountMerge.source_account_id == current,
            )
        ).first()
        if row is None:
            return current
        destination = canonical_account_id(row.destination_account_id)
        if destination in seen:
            # A cycle (alice → bob → alice): stop rather than chase it.
            return current
        current = destination
        seen.add(current)
