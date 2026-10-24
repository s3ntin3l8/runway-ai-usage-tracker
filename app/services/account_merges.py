"""Durable account merges: where a retired account id went.

The account merge moves an account's history, credential sources and tags to
the destination and retires the source id. The machine, however, keeps
resolving its credential to the source id and pushes it every cycle — so
without a record of the move the source account comes back, with a live
credential, and the destination is left with a credential-source row it
cannot collect with.

This module holds that record and the one lookup the ingest needs, plus the
reads that tell the operator where a merged-away account went — and the
journal of what each merge moved, so a merge can be reversed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.models._datetime import iso_utc
from app.models.db import (
    CredentialSource,
    CredentialTag,
    LatestUsage,
    LatestUsageContribution,
    ProviderAccountMerge,
    ProviderConfig,
    QuotaSnapshot,
    SidecarSourceContribution,
    UsageEvent,
    UsageWindow,
)
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


# ---------------------------------------------------------------------------
# The un-merge
# ---------------------------------------------------------------------------


class UnmergeRefusedError(Exception):
    """The un-merge cannot proceed.

    The endpoint turns it into a 409 (or a 404 when there is no merge to
    undo): the refusal is the answer, not a failure to hide.
    """

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class UnmergePlan:
    """What an un-merge would put back — and what it cannot."""

    provider_id: str
    source_account_id: str
    destination_account_id: str
    rows: dict[str, int] = field(default_factory=dict)
    credential_sources: list[str] = field(default_factory=list)
    credential_tags: int = 0
    contributions: int = 0
    config: str = "absent"  # "renamed" | "archived" | "absent"
    collisions: int = 0
    rows_in_the_way: dict[str, int] = field(default_factory=dict)
    not_restored: list[str] = field(default_factory=list)


def _merge_record(
    session: Session, provider_id: str, source_account_id: str
) -> ProviderAccountMerge:
    """The merge record of one account, or a refusal: there is nothing to undo."""
    row = session.exec(
        select(ProviderAccountMerge).where(
            ProviderAccountMerge.provider_id == provider_id,
            ProviderAccountMerge.source_account_id == canonical_account_id(source_account_id),
        )
    ).first()
    if row is None:
        raise UnmergeRefusedError(
            f"No merge is recorded for {canonical_account_id(source_account_id)}.",
            status_code=404,
        )
    return row


def _unmerge_journal(record: ProviderAccountMerge) -> MergeJournal:
    """The journal of the merge, or a refusal: it cannot be reversed."""
    journal = MergeJournal.from_json(record.moved)
    if journal is None:
        raise UnmergeRefusedError(
            "This merge cannot be reversed: it was recorded before the merge kept a "
            "journal of what it moved."
        )
    return journal


def _rows_in_the_way(
    session: Session,
    provider_id: str,
    source: str,
    journal: MergeJournal,
) -> dict[str, list[int]]:
    """The rows the source holds now that a row the merge moved would collide with.

    The merge moved the source's rows to the destination, so the source
    normally holds nothing and nothing is in the way. A row in the way is an
    account that was recreated under the same id while the merge was in force
    — the guard that refuses to resurrect a merged-away account makes that
    unreachable through the API, but the un-merge does not assume it.

    Keyed by table: the row ids to drop before the moved rows go back. The
    original history wins — restoring it is the point of the un-merge — so the
    row in the way is the one that goes.
    """
    in_the_way: dict[str, list[int]] = {}
    for table, ids in journal.rows.items():
        model = MERGE_MODELS_BY_TABLE.get(table)
        if model is None or not ids:
            continue
        moving = session.exec(select(model).where(model.id.in_(ids))).all()
        if not moving:
            continue
        existing = session.exec(
            select(model).where(model.provider_id == provider_id, model.account_id == source)
        ).all()
        if not existing:
            continue
        keys = row_identity_keys(model)
        if not keys:
            continue
        blocked: list[int] = []
        for row in existing:
            for key in keys:
                if any(
                    tuple(getattr(moving_row, name) for name in key)
                    == tuple(getattr(row, name) for name in key)
                    for moving_row in moving
                ):
                    blocked.append(row.id)
                    break
        if blocked:
            in_the_way[table] = blocked
    return in_the_way


def _not_restored(journal: MergeJournal) -> list[str]:
    """What the merge destroyed, and the un-merge cannot bring back."""
    lost: list[str] = []
    if journal.collisions_removed:
        lost.append(
            f"{journal.collisions_removed} duplicate rows the merge dropped (they were deleted)"
        )
    if journal.config_outcome == "archived":
        if journal.had_credential is True:
            lost.append(
                "the stored API key / session cookie the merge wiped from the account's provider row"
            )
        elif journal.had_credential is None:
            # A journal from before the merge recorded whether the account held
            # a secret: the wipe is known, the secret is not.
            lost.append(
                "the stored API key / session cookie the merge wiped from the account's provider "
                "row, if the account held one"
            )
    return lost


def _config_outcome(session: Session, journal: MergeJournal) -> tuple[str, bool]:
    """What will happen to the account's provider row, and whether it is gone.

    The journal says what the merge did to the row. Whether the row is still
    there is a separate question — the operator may have deleted it since —
    and the preview must not claim a restoration that cannot happen.
    """
    if journal.config_outcome == "absent" or journal.config_row_id is None:
        return "absent", False
    row = session.exec(
        select(ProviderConfig).where(ProviderConfig.id == journal.config_row_id)
    ).first()
    if row is None:
        return "absent", True
    return journal.config_outcome, False


def _unmerge_chain(session: Session, provider_id: str, destination: str) -> str | None:
    """The account the destination itself was merged into, if any.

    A chain is unwound from the end: undoing the middle link would rewrite
    the chain twice, and the operator's model of it would not survive it.
    """
    return is_merged_away(session, provider_id, destination)


def _assert_the_account_id_is_free(
    session: Session, provider_id: str, source: str, journal: MergeJournal
) -> None:
    """Refuse when the account id has been taken again.

    The merge renamed the account's provider row to the destination; putting
    it back needs the id to be free. A row that took it in the meantime means
    two accounts would answer to one id.
    """
    if journal.config_outcome != "renamed" or journal.config_row_id is None:
        return
    row = session.exec(
        select(ProviderConfig).where(ProviderConfig.id == journal.config_row_id)
    ).first()
    if row is None:
        return
    clash = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == provider_id,
            ProviderConfig.account_id == source,
        )
    ).first()
    if clash is not None and clash.id != row.id:
        raise UnmergeRefusedError(f"The account id {source} is taken again; it cannot be restored.")


@dataclass
class _UnmergeFacts:
    """What one un-merge works from — read once, used for the plan and the moves.

    The preview and the apply read the same facts: the record, the journal, the
    rows that go back and the rows that got in the way. Reading them twice
    would scan every merge table twice, and — worse — could read two different
    worlds.
    """

    record: ProviderAccountMerge
    journal: MergeJournal
    source: str
    destination: str
    #: The rows that go back, by table — only the tables this build knows.
    rows: dict[str, list[int]]
    #: The rows that got in the way, by table: to drop before the rows go back.
    in_the_way: dict[str, list[int]]
    config: str
    not_restored: list[str]

    @property
    def collisions(self) -> int:
        return sum(len(ids) for ids in self.in_the_way.values())


def _build_facts(session: Session, provider_id: str, source_account_id: str) -> _UnmergeFacts:
    """Read everything the un-merge needs, and refuse what cannot be undone."""
    record = _merge_record(session, provider_id, source_account_id)
    journal = _unmerge_journal(record)
    source = canonical_account_id(record.source_account_id)
    destination = canonical_account_id(record.destination_account_id)

    chain = _unmerge_chain(session, provider_id, destination)
    if chain:
        raise UnmergeRefusedError(
            f"The destination was merged away too (into {chain}); undo the chain from the end."
        )

    # Only the tables both ends know. A table the journal names that this
    # build does not know cannot be moved back, so it must not be promised.
    rows = {
        table: list(ids)
        for table, ids in journal.rows.items()
        if ids and table in MERGE_MODELS_BY_TABLE
    }
    config, row_gone = _config_outcome(session, journal)
    not_restored = _not_restored(journal)
    if row_gone:
        not_restored.append(
            "the account's provider row is gone; its settings have to be added again"
        )
    return _UnmergeFacts(
        record=record,
        journal=journal,
        source=source,
        destination=destination,
        rows=rows,
        in_the_way=_rows_in_the_way(session, provider_id, source, journal),
        config=config,
        not_restored=not_restored,
    )


def _plan(facts: _UnmergeFacts) -> UnmergePlan:
    """The public shape of what an un-merge does."""
    return UnmergePlan(
        provider_id=facts.record.provider_id,
        source_account_id=facts.source,
        destination_account_id=facts.destination,
        rows={table: len(ids) for table, ids in facts.rows.items()},
        credential_sources=list(facts.journal.credential_sources),
        credential_tags=len(facts.journal.credential_tags),
        contributions=len(facts.journal.contributions),
        config=facts.config,
        collisions=facts.collisions,
        rows_in_the_way={table: len(ids) for table, ids in facts.in_the_way.items()},
        not_restored=list(facts.not_restored),
    )


def preview_unmerge(session: Session, provider_id: str, source_account_id: str) -> UnmergePlan:
    """What undoing one merge would put back.

    Raises :class:`UnmergeRefusedError` when the merge cannot be undone at all.
    """
    return _plan(_build_facts(session, provider_id, source_account_id))


def unmerge_account(
    session: Session,
    provider_id: str,
    source_account_id: str,
    *,
    confirm_collisions: bool = False,
    previewed_collisions: int | None = None,
) -> UnmergePlan:
    """Put a merged account back: its history, its credential and its name.

    The rows go back to the account they came from, the credential sources
    and the durable contributions follow, the provider row is restored, and
    the merge record is deleted — so the source id names an account again.

    ``previewed_collisions`` is the number the preview showed. If the rows in
    the way changed since, the confirmation the operator gave was for a
    different set of rows — and the un-merge refuses.
    """
    facts = _build_facts(session, provider_id, source_account_id)
    plan = _plan(facts)
    if previewed_collisions is not None and plan.collisions != previewed_collisions:
        raise UnmergeRefusedError(
            f"The rows in the way changed since the preview ({previewed_collisions} then, "
            f"{plan.collisions} now); preview again."
        )
    if plan.collisions and not confirm_collisions:
        raise UnmergeRefusedError(
            f"{plan.collisions} rows would collide with rows the account holds now; "
            "preview and explicitly confirm the collision removal."
        )

    # Every refusal is decided before anything is touched: an un-merge that
    # half-happened would be worse than one that did not happen.
    _assert_the_account_id_is_free(session, provider_id, facts.source, facts.journal)

    source = facts.source
    destination = facts.destination
    journal = facts.journal

    # The rows in the way go first, and the delete is flushed before the rows
    # that take their place: the unique constraint is on the account id, so
    # the row that is in the way has to be gone before a row with the same
    # identity moves into the account.
    for table, blocked_ids in facts.in_the_way.items():
        model = MERGE_MODELS_BY_TABLE.get(table)
        if model is None:
            continue
        for row in session.exec(select(model).where(model.id.in_(blocked_ids))).all():
            session.delete(row)
    if facts.in_the_way:
        session.flush()

    # The account-scoped rows: the merge moved them, the un-merge moves them back.
    for table, row_ids in facts.rows.items():
        model = MERGE_MODELS_BY_TABLE.get(table)
        if model is None or not row_ids:
            continue
        for row in session.exec(select(model).where(model.id.in_(row_ids))).all():
            row.account_id = source

    # The credential sources: the machine's credential follows the account back.
    for credential_source_id in journal.credential_sources:
        rows = session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.source_id == credential_source_id,
            )
        ).all()
        for row in rows:
            row.account_id = source

    # The operator tags.
    for tag_id in journal.credential_tags:
        row = session.exec(select(CredentialTag).where(CredentialTag.id == tag_id)).first()
        if row is not None:
            row.account_id = source

    # The durable contributions — the credential at rest.
    for contribution_id in journal.contributions:
        row = session.exec(
            select(SidecarSourceContribution).where(SidecarSourceContribution.id == contribution_id)
        ).first()
        if row is not None:
            row.account_id = source

    # The provider row: the merge renamed it to the destination, or archived
    # it. Both are undone here — except the credential the merge wiped, which
    # is gone for good and which the preview says so.
    if journal.config_row_id is not None:
        row = session.exec(
            select(ProviderConfig).where(ProviderConfig.id == journal.config_row_id)
        ).first()
        if row is not None:
            if journal.config_outcome == "renamed":
                row.account_id = source
            elif journal.config_outcome == "archived":
                # The flags the row carried *before* the merge — the merge's
                # own archive must not be confused with an archive the
                # operator did earlier.
                if journal.config_archived is not None:
                    row.archived = journal.config_archived
                if journal.config_enabled is not None:
                    row.enabled = journal.config_enabled

    # The record goes: the source id names an account again, and the guard
    # that refused to make it one stops refusing.
    session.delete(facts.record)
    session.flush()

    # The rollups are derived from the usage events: rebuild both accounts'
    # views instead of editing them.
    from app.services.period_rollups import rebuild_rollups_for_pairs

    rebuild_rollups_for_pairs(session, {(provider_id, source), (provider_id, destination)})
    return plan
