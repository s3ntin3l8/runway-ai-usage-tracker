"""Durable account merges: where a retired account id went.

The account merge moves an account's history, credential sources and tags to
the destination and retires the source id. The machine, however, keeps
resolving its credential to the source id and pushes it every cycle — so
without a record of the move the source account comes back, with a live
credential, and the destination is left with a credential-source row it
cannot collect with.

This module holds that record and the one lookup the ingest needs, plus the
reads that tell the operator where a merged-away account went — and the
journal of what each merge moved, so a merge can be reversed. The reversal
itself lives in :mod:`app.services.account_unmerge` and is re-exported here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.models.db import (
    LatestUsage,
    LatestUsageContribution,
    ProviderAccountMerge,
    QuotaSnapshot,
    UsageEvent,
    UsageWindow,
)
from app.models.utc_datetime import iso_utc
from app.services.account_identity import canonical_account_id

logger = logging.getLogger(__name__)


@dataclass
class MergeJournal:
    """What one merge moved, so that the move can be reversed.

    The rows are recorded by **id**: once a row sits under the destination it
    is indistinguishable from the destination's own row, so an account id
    alone cannot say which rows the merge carried over.

    What the merge destroyed is not journalled: the duplicates it deleted are
    gone, and so is the provider credential it wiped.
    """

    rows: dict[str, list[int]] = field(default_factory=dict)
    credential_sources: list[str] = field(default_factory=list)
    credential_tags: list[int] = field(default_factory=list)
    contributions: list[int] = field(default_factory=list)
    config_row_id: int | None = None
    config_outcome: str = "absent"  # "renamed" | "archived" | "absent"
    config_enabled: bool | None = None
    config_archived: bool | None = None
    #: Whether the account held a stored secret when the merge archived its
    #: row. The merge wiped the secret either way, but "wiped a secret there
    #: was" and "wiped nothing" are not the same loss — and the preview must
    #: only claim what is true. ``None`` is a journal from before this was
    #: recorded: the wipe is known, the secret is not.
    had_credential: bool | None = None
    collisions_removed: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "rows": self.rows,
                "credential_sources": self.credential_sources,
                "credential_tags": self.credential_tags,
                "contributions": self.contributions,
                "config_row_id": self.config_row_id,
                "config_outcome": self.config_outcome,
                "config_enabled": self.config_enabled,
                "config_archived": self.config_archived,
                "had_credential": self.had_credential,
                "collisions_removed": self.collisions_removed,
            }
        )

    @classmethod
    def from_json(cls, raw: str | None) -> MergeJournal | None:
        """The journal of a stored merge, or None when it has none.

        A merge performed before the journal existed has no journal: it cannot
        be reversed, and saying so beats pretending.
        """
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("A merge journal is not readable JSON; treating it as absent.")
            return None
        if not isinstance(data, dict):
            logger.warning("A merge journal is not an object; treating it as absent.")
            return None
        try:
            return cls(
                rows={str(k): [int(i) for i in v] for k, v in (data.get("rows") or {}).items()},
                credential_sources=[str(s) for s in (data.get("credential_sources") or [])],
                credential_tags=[int(i) for i in (data.get("credential_tags") or [])],
                contributions=[int(i) for i in (data.get("contributions") or [])],
                config_row_id=data.get("config_row_id"),
                config_outcome=str(data.get("config_outcome") or "absent"),
                config_enabled=data.get("config_enabled"),
                config_archived=data.get("config_archived"),
                had_credential=data.get("had_credential"),
                collisions_removed=int(data.get("collisions_removed") or 0),
            )
        except (TypeError, ValueError):
            logger.warning("A merge journal holds values that are not what it should; absent.")
            return None


#: The account-scoped tables a merge moves — and the un-merge puts back.
#: One list for both: a table that one of them knows and the other does not
#: would lose its rows without a word.
MERGE_MODELS: tuple[Any, ...] = (
    UsageEvent,
    LatestUsage,
    LatestUsageContribution,
    QuotaSnapshot,
    UsageWindow,
)

MERGE_MODELS_BY_TABLE: dict[str, Any] = {model.__tablename__: model for model in MERGE_MODELS}


def row_identity_keys(model: Any) -> list[tuple[str, ...]]:
    """The logical identity of a row: its unique constraint without the
    provider, the account and the row id.

    Two rows collide when their identity matches — the merge's own rule, so
    the un-merge spots a collision exactly where the merge would have made
    one. The merge reads this too, so the two cannot drift apart.
    """
    unique_sets = [
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    unique_sets.extend(
        tuple(column.name for column in index.columns)
        for index in model.__table__.indexes
        if index.unique
    )
    keys = [
        tuple(name for name in names if name not in ("provider_id", "account_id", "id"))
        for names in unique_sets
    ]
    return [key for key in keys if key]


def record_account_merge(
    session: Session,
    provider_id: str,
    source_account_id: str,
    destination_account_id: str,
    journal: MergeJournal | None = None,
) -> None:
    """Record that the source account went to the destination.

    One row per (provider, source account): merging the same source again
    replaces the earlier record — the latest merge wins. The journal of what
    the merge moved is stored with it, so the merge can be reversed.
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
                moved=journal.to_json() if journal is not None else None,
            )
        )
    else:
        row.destination_account_id = destination
        row.moved = journal.to_json() if journal is not None else None
    session.flush()


def load_journal(session: Session, provider_id: str, source_account_id: str) -> MergeJournal | None:
    """The journal of one merge, or None when it has none (or no record)."""
    row = session.exec(
        select(ProviderAccountMerge).where(
            ProviderAccountMerge.provider_id == provider_id,
            ProviderAccountMerge.source_account_id == canonical_account_id(source_account_id),
        )
    ).first()
    if row is None:
        return None
    return MergeJournal.from_json(row.moved)


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


# The un-merge lives in account_unmerge. Imported after the journal helpers
# above exist, so this module can re-export it without a cycle.
from app.services.account_unmerge import (
    UnmergePlan,
    UnmergeRefusedError,
    preview_unmerge,
    unmerge_account,
)

__all__ = [
    "MERGE_MODELS",
    "MERGE_MODELS_BY_TABLE",
    "MergeJournal",
    "MergedAccount",
    "UnmergePlan",
    "UnmergeRefusedError",
    "is_merged_away",
    "load_journal",
    "merged_account_map",
    "merged_account_rows",
    "merged_accounts",
    "preview_unmerge",
    "record_account_merge",
    "resolve_merged_account",
    "row_identity_keys",
    "unmerge_account",
]
