"""Put a merged account back.

The merge journal — what each merge moved — lives in
:mod:`app.services.account_merges`. This module reads that journal and
reverses the move: the preview says what would come back, and the apply
puts the history, the credential and the name back on the source account.

The public names are re-exported from ``account_merges`` so existing
callers keep importing them from there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models.db import (
    CredentialSource,
    CredentialTag,
    ProviderAccountMerge,
    ProviderConfig,
    SidecarSourceContribution,
)
from app.services.account_identity import canonical_account_id


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


# Journal helpers stay in account_merges. Imported here, after the un-merge
# names exist, so the two modules can import each other without a cycle:
# account_merges re-exports these names only once its journal helpers exist.
from app.services.account_merges import (
    MERGE_MODELS_BY_TABLE,
    MergeJournal,
    is_merged_away,
    row_identity_keys,
)
