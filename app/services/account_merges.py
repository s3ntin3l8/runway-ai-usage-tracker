"""Durable account merges: where a retired account id went.

The account merge moves an account's history, credential sources and tags to
the destination and retires the source id. The machine, however, keeps
resolving its credential to the source id and pushes it every cycle — so
without a record of the move the source account comes back, with a live
credential, and the destination is left with a credential-source row it
cannot collect with.

This module holds that record and the one lookup the ingest needs, plus the
reads that tell the operator where a merged-away account went.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.models._datetime import iso_utc
from app.models.db import ProviderAccountMerge
from app.services.account_identity import canonical_account_id

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class MergedAccount:
    """An account that was merged away, and the account it became.

    ``destination_account_id`` is where the account *ended up*: a chain is
    followed to its end (alice → bob → carol is carol), because that is what
    the account is now and what the ingest resolves a claim of it to.
    """

    provider_id: str
    account_id: str
    destination_account_id: str
    merged_at: str


def merged_account_rows(session: Session) -> list[ProviderAccountMerge]:
    """Every merge record.

    A database that predates the merge table has no merges to show, and the
    page that shows them must still render — a missing table is an empty
    list, not a 500.
    """
    try:
        return list(session.exec(select(ProviderAccountMerge)).all())
    except OperationalError:
        logger.debug("The account-merge table is not present; no merges to show.")
        return []


def _chain_destinations(rows: list[ProviderAccountMerge]) -> dict[tuple[str, str], str]:
    """Where each merged-away id ended up, following the chain.

    The same rule :func:`resolve_merged_account` applies on the ingest path:
    a cycle stops at the last visited id instead of looping.
    """
    direct: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row.provider_id, canonical_account_id(row.source_account_id))
        direct[key] = canonical_account_id(row.destination_account_id)
    resolved: dict[tuple[str, str], str] = {}
    for (provider_id, source), destination in direct.items():
        current = destination
        seen = {source}
        while True:
            following = direct.get((provider_id, current))
            if following is None or following in seen:
                break
            current = following
            seen.add(current)
        resolved[(provider_id, source)] = current
    return resolved


def merged_accounts(session: Session) -> list[MergedAccount]:
    """Every merge, with the account it ended up as. One query for them all."""
    rows = merged_account_rows(session)
    destinations = _chain_destinations(rows)
    merged: list[MergedAccount] = []
    for row in rows:
        account_id = canonical_account_id(row.source_account_id)
        merged.append(
            MergedAccount(
                provider_id=row.provider_id,
                account_id=account_id,
                destination_account_id=destinations.get(
                    (row.provider_id, account_id),
                    canonical_account_id(row.destination_account_id),
                ),
                merged_at=iso_utc(row.merged_at) or "",
            )
        )
    return merged


def merged_account_map(session: Session) -> dict[tuple[str, str], str]:
    """(provider_id, account_id) -> the account it ended up as."""
    return {
        (m.provider_id, m.account_id): m.destination_account_id for m in merged_accounts(session)
    }


def is_merged_away(session: Session, provider_id: str, account_id: str) -> str | None:
    """The account a merged-away id ended up as, or None when it never was.

    The account that was merged *into* is not merged away: it stays a normal
    account, and only the id that was merged away is gone.
    """
    current = canonical_account_id(account_id)
    destination = resolve_merged_account(session, provider_id, current)
    return destination if destination != current else None
