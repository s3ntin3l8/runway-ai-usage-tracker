import logging
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.core.utils import scrub_log
from app.models.db import (
    CredentialSource,
    ProviderAccountLabel,
    ProviderConfig,
)
from app.services import audit_log, auth_failures, source_controls
from app.services.account_identity import (
    canonical_account_id,
)
from app.services.account_merges import (
    MERGE_MODELS,
    MergeJournal,
    UnmergePlan,
    UnmergeRefusedError,
    preview_unmerge,
    record_account_merge,
    resolve_merged_account,
    row_identity_keys,
    unmerge_account,
)
from app.services.collector_manager import manager
from app.services.credential_tags import CredentialTagRepo
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)
router = APIRouter()


class _AccountMergeRequest(BaseModel):
    provider_id: str
    source_account_id: str
    destination_account_id: str
    confirm_collisions: bool = False
    confirm_shared_default: bool = False


def _account_merge_rows(
    session: Session, provider_id: str, source_id: str, destination_id: str
) -> list[tuple[Any, Sequence[Any], list[Any]]]:
    """Load account scoped records and identify duplicate logical identities."""
    result: list[tuple[Any, Sequence[Any], list[Any]]] = []
    for model in MERGE_MODELS:
        source = session.exec(
            select(model).where(model.provider_id == provider_id, model.account_id == source_id)
        ).all()
        dest = session.exec(
            select(model).where(
                model.provider_id == provider_id, model.account_id == destination_id
            )
        ).all()
        keys = row_identity_keys(model)
        # A row is colliding if any declared uniqueness identity matches.
        collisions = []
        for row in source:
            if any(
                tuple(getattr(row, name) for name in key)
                in {tuple(getattr(candidate, name) for name in key) for candidate in dest}
                for key in keys
            ):
                collisions.append(row)
        result.append((model, source, collisions))
    return result


def _reject_merged_away_accounts(
    session: Session, provider_id: str, source_id: str, destination_id: str
) -> None:
    """Refuse a merge whose source or destination was already merged away.

    A merge is a durable claim on the source account id — the machine keeps
    resolving its credential to it, so the ingest resolves it to the
    destination. Merging an account that was already merged away, in either
    direction, would leave two records pointing at each other.
    """
    if resolve_merged_account(session, provider_id, source_id) != source_id:
        raise HTTPException(
            status_code=409,
            detail="The source account has been merged away; merge the account it went to instead.",
        )
    if resolve_merged_account(session, provider_id, destination_id) != destination_id:
        raise HTTPException(
            status_code=409,
            detail="The destination account has been merged away; merge into the account it went to instead.",
        )


@router.post("/provider-account-merge/preview")
@limiter.limit("20/minute")
async def preview_provider_account_merge(
    request: Request,
    body: _AccountMergeRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    if body.provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail="Unknown provider")
    source_id = canonical_account_id(body.source_account_id)
    destination_id = canonical_account_id(body.destination_account_id)
    if source_id == destination_id:
        raise HTTPException(status_code=400, detail="Source and destination must differ")
    _reject_merged_away_accounts(session, body.provider_id, source_id, destination_id)
    tables = _account_merge_rows(session, body.provider_id, source_id, destination_id)
    counts = {
        model.__tablename__: {"affected": len(rows), "collisions": len(collisions)}
        for model, rows, collisions in tables
    }
    from app.models.db import UsagePeriodRollup

    counts["usage_period_rollup"] = {
        "affected": len(
            session.exec(
                select(UsagePeriodRollup).where(
                    UsagePeriodRollup.provider_id == body.provider_id,
                    UsagePeriodRollup.account_id == source_id,
                )
            ).all()
        ),
        "collisions": 0,
    }
    source_credentials = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == body.provider_id,
            CredentialSource.account_id == source_id,
        )
    ).all()
    destination_source_ids = {
        row.source_id
        for row in session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == body.provider_id,
                CredentialSource.account_id == destination_id,
            )
        ).all()
    }
    counts["credential_sources"] = {
        "affected": len(source_credentials),
        "collisions": sum(row.source_id in destination_source_ids for row in source_credentials),
    }
    counts["credential_tags"] = {
        "affected": sum(
            1
            for tag in CredentialTagRepo.list_for_account_provider(
                session, provider_id=body.provider_id
            )
            if tag.account_id == source_id
        ),
        "collisions": 0,
    }
    return {
        "provider_id": body.provider_id,
        "source_account_id": source_id,
        "destination_account_id": destination_id,
        "counts": counts,
        "total_collisions": sum(item["collisions"] for item in counts.values()),
        "shared_default_warning": source_id == "default" or destination_id == "default",
    }


@router.post("/provider-account-merge/apply")
@limiter.limit("10/minute")
async def apply_provider_account_merge(
    request: Request,
    body: _AccountMergeRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    if body.provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail="Unknown provider")
    source_id, destination_id = (
        canonical_account_id(body.source_account_id),
        canonical_account_id(body.destination_account_id),
    )
    if source_id == destination_id:
        raise HTTPException(status_code=400, detail="Source and destination must differ")
    _reject_merged_away_accounts(session, body.provider_id, source_id, destination_id)
    if (source_id == "default" or destination_id == "default") and not body.confirm_shared_default:
        raise HTTPException(
            status_code=409,
            detail="Merging with the shared default identity can affect unattributed usage; preview and explicitly confirm.",
        )
    tables = _account_merge_rows(session, body.provider_id, source_id, destination_id)
    credential_sources = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == body.provider_id,
            CredentialSource.account_id == source_id,
        )
    ).all()
    destination_source_ids = {
        row.source_id
        for row in session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == body.provider_id,
                CredentialSource.account_id == destination_id,
            )
        ).all()
    }
    collisions = sum(len(dupes) for _, _, dupes in tables) + sum(
        row.source_id in destination_source_ids for row in credential_sources
    )
    if collisions and not body.confirm_collisions:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Merge has duplicate history; preview and explicitly confirm collision removal.",
                "collisions": collisions,
            },
        )
    # The journal of what this merge moves, so it can be reversed. The rows
    # are recorded by id: once a row sits under the destination it cannot be
    # told apart from the destination's own row.
    journal = MergeJournal()
    for model, rows, dupes in tables:
        duplicate_ids = {row.id for row in dupes}
        moved_ids: list[int] = []
        for row in rows:
            if row.id in duplicate_ids:
                session.delete(row)
            else:
                row.account_id = destination_id
                if row.id is not None:
                    moved_ids.append(row.id)
        journal.rows[model.__tablename__] = moved_ids
    # Rollups are derived from UsageEvent. Rebuild both account views after
    # moving/deduplicating authoritative events instead of editing rollups.
    from app.services.period_rollups import rebuild_rollups_for_pairs

    rebuild_rollups_for_pairs(
        session, {(body.provider_id, source_id), (body.provider_id, destination_id)}
    )
    # Keep destination credential settings authoritative. Re-home nonconflicting
    # source metadata. Shared default identity tags are deliberately not globalized.
    for row in credential_sources:
        if row.source_id in destination_source_ids:
            session.delete(row)
        else:
            row.account_id = destination_id
            journal.credential_sources.append(row.source_id)
    if source_id != "default":
        for row in CredentialTagRepo.list_for_account_provider(
            session, provider_id=body.provider_id
        ):
            if row.account_id == source_id:
                row.account_id = destination_id
                if row.id is not None:
                    journal.credential_tags.append(row.id)
    src = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == body.provider_id, ProviderConfig.account_id == source_id
        )
    ).first()
    if src:
        destination_config = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == body.provider_id,
                ProviderConfig.account_id == destination_id,
            )
        ).first()
        # The flags before the merge: the un-merge restores them. Both of
        # them — the merge's own archive must not be confused with an archive
        # the operator did earlier.
        journal.config_row_id = src.id
        journal.config_enabled = src.enabled
        journal.config_archived = src.archived
        if destination_config is None:
            src.account_id = destination_id
            journal.config_outcome = "renamed"
        else:
            src.enabled = False
            src.archived = True
            # Whether the account held a secret at all: the merge wipes the
            # secret either way, but "wiped a secret there was" and "wiped
            # nothing" are not the same loss, and the un-merge's preview must
            # only claim what is true.
            journal.had_credential = bool(src.api_key_encrypted or src.session_cookie_encrypted)
            src.api_key_encrypted = None
            src.session_cookie_encrypted = None
            journal.config_outcome = "archived"
    # The merge moves the account, so the credential that belonged to it
    # follows: the durable contribution is re-filed under the destination (the
    # cache reloads every durable bundle on the next revision change, so the
    # re-filing is what moves the bundle), and the record tells the ingest to
    # resolve the machine's claim of the source id to the destination — without
    # it the source account comes back on the next push, with a live
    # credential. Part of the merge transaction: a failed commit leaves the
    # source account and its credential alone.
    journal.contributions = source_controls.move_contributions_for_account(
        session, body.provider_id, source_id, destination_id
    )
    journal.collisions_removed = collisions
    record_account_merge(session, body.provider_id, source_id, destination_id, journal)
    session.commit()
    # The credential follows the account into the destination; whatever is
    # left under the retired source id is stale and goes — the same rule the
    # archive write path follows. Evicted after the commit, like the DELETE
    # path: a failed commit must not leave a live account without its
    # credential.
    for moved_source in credential_sources:
        await token_cache.move_source(
            body.provider_id, source_id, destination_id, moved_source.source_id
        )
    await token_cache.remove(body.provider_id, source_id)
    auth_failures.clear(body.provider_id, source_id)
    manager._last_sync_time = 0.0
    for account_id in (source_id, destination_id):
        try:
            await manager.reset_collector(body.provider_id, account_id)
        except Exception:
            logger.exception(
                "Could not reset collector after account merge: %s/%s",
                scrub_log(body.provider_id),
                scrub_log(account_id),
            )
    from app.core.cache import cache_clear
    from app.services.poller import poller

    cache_clear()
    poller.wake()
    audit_log.record(
        session,
        request,
        action="provider.account_merge",
        target_id=f"{body.provider_id}/{destination_id}",
        payload={"source_account_id": source_id, "collisions_removed": collisions},
    )
    return {
        "status": "merged",
        "provider_id": body.provider_id,
        "source_account_id": source_id,
        "destination_account_id": destination_id,
        "collisions_removed": collisions,
    }


class _AccountUnmergeRequest(BaseModel):
    provider_id: str
    source_account_id: str
    confirm_collisions: bool = False
    #: The number of rows in the way the preview showed. The rows may have
    #: changed since — and a confirmation the operator gave for a different
    #: set of rows is not a confirmation of these.
    previewed_collisions: int = 0


def _unmerge_response(plan: UnmergePlan) -> dict[str, Any]:
    return {
        "provider_id": plan.provider_id,
        "source_account_id": plan.source_account_id,
        "destination_account_id": plan.destination_account_id,
        "rows": plan.rows,
        "credential_sources": plan.credential_sources,
        "credential_tags": plan.credential_tags,
        "contributions": plan.contributions,
        "config": plan.config,
        "total_collisions": plan.collisions,
        "rows_in_the_way": plan.rows_in_the_way,
        "not_restored": plan.not_restored,
    }


@router.post("/provider-account-unmerge/preview")
@limiter.limit("20/minute")
async def preview_provider_account_unmerge(
    request: Request,
    body: _AccountUnmergeRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """What undoing one merge would put back — and what it cannot.

    A merge moves the account's history and its credential to the
    destination. Undoing it puts them back; what the merge destroyed (the
    duplicates it dropped, the stored credential it wiped) does not come
    back, and the preview says so rather than losing it silently.
    """
    if body.provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail="Unknown provider")
    try:
        plan = preview_unmerge(session, body.provider_id, body.source_account_id)
    except UnmergeRefusedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _unmerge_response(plan)


@router.post("/provider-account-unmerge/apply")
@limiter.limit("10/minute")
async def apply_provider_account_unmerge(
    request: Request,
    body: _AccountUnmergeRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Undo one merge: the account's history, its credential and its name go
    back to the account they came from.

    The rows the merge moved are recorded by id, so the un-merge knows
    exactly which rows to put back — the destination's own rows stay.
    """
    if body.provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail="Unknown provider")
    try:
        plan = unmerge_account(
            session,
            body.provider_id,
            body.source_account_id,
            confirm_collisions=body.confirm_collisions,
            previewed_collisions=body.previewed_collisions,
        )
    except UnmergeRefusedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    session.commit()
    # The cached bundle goes back with the credential: the durable
    # contribution has been re-filed already, and the cache reloads every
    # durable bundle on the next revision change.
    for credential_source_id in plan.credential_sources:
        await token_cache.move_source(
            body.provider_id,
            plan.destination_account_id,
            plan.source_account_id,
            credential_source_id,
        )
    auth_failures.clear(body.provider_id, plan.source_account_id)
    manager._last_sync_time = 0.0
    for account_id in (plan.source_account_id, plan.destination_account_id):
        try:
            await manager.reset_collector(body.provider_id, account_id)
        except Exception:
            logger.exception(
                "Could not reset collector after account unmerge: %s/%s",
                scrub_log(body.provider_id),
                scrub_log(account_id),
            )
    from app.core.cache import cache_clear
    from app.services.poller import poller

    cache_clear()
    poller.wake()
    audit_log.record(
        session,
        request,
        action="provider.account_unmerge",
        target_id=f"{body.provider_id}/{plan.source_account_id}",
        payload={
            "destination_account_id": plan.destination_account_id,
            # The destructive part of the un-merge: how many rows it dropped
            # to make room for the rows the merge moved.
            "collisions_removed": plan.collisions,
        },
    )
    return {"status": "unmerged", **_unmerge_response(plan)}


class _DiscoveredAccountLabelUpdate(BaseModel):
    account_label: str | None = Field(max_length=120)


@router.put("/provider-account-label/{provider_id}/{account_id}")
@limiter.limit("20/minute")
async def put_discovered_account_label(
    request: Request,
    provider_id: str,
    account_id: str,
    body: _DiscoveredAccountLabelUpdate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, str]:
    """Save a display label independently of provider collection settings."""
    if provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")
    account_id = canonical_account_id(account_id)
    row = session.exec(
        select(ProviderAccountLabel).where(
            ProviderAccountLabel.provider_id == provider_id,
            ProviderAccountLabel.account_id == account_id,
        )
    ).first()
    label = body.account_label.strip() if body.account_label else None
    if row is None:
        row = ProviderAccountLabel(
            provider_id=provider_id, account_id=account_id, account_label=label
        )
        session.add(row)
    else:
        row.account_label = label
    session.commit()
    return {"status": "saved", "provider_id": provider_id, "account_id": account_id}


__all__ = [
    "_AccountMergeRequest",
    "_AccountUnmergeRequest",
    "_DiscoveredAccountLabelUpdate",
    "_account_merge_rows",
    "_reject_merged_away_accounts",
    "_unmerge_response",
    "apply_provider_account_merge",
    "apply_provider_account_unmerge",
    "preview_provider_account_merge",
    "preview_provider_account_unmerge",
    "put_discovered_account_label",
]
