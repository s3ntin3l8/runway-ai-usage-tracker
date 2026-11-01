import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.core.utils import has_refresh_credential, scrub_log
from app.models.db import (
    CredentialSource,
)
from app.models.schemas import CredentialInventory
from app.services import audit_log, auth_failures
from app.services.account_identity import (
    canonical_account_id,
)
from app.services.credential_inventory import build_inventory as build_credential_inventory
from app.services.credential_sources import (
    is_server_source_id,
)
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/credentials", response_model=CredentialInventory)
@limiter.limit("30/minute")
async def get_credential_inventory(
    request: Request,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> CredentialInventory:
    """Every discovered credential — provider → account → source — with the machine it
    came from, why it maps to its account, its status, and which source is currently
    feeding the data. Never returns secret values."""
    from app.api.endpoints.fleet import visible_pending_rows
    from app.services.blocked_collection import blocked_collection
    from app.services.blocked_sources import source_block_rows, source_blocks

    inventory = await build_credential_inventory()
    # "Unmapped" means what the Untagged list shows: rows that already have an effective
    # hint are not waiting for anyone, so they neither count nor warn.
    visible = visible_pending_rows(session)
    per_machine: dict[str, int] = {}
    for row in visible:
        per_machine[row.sidecar_id] = per_machine.get(row.sidecar_id, 0) + 1
    inventory.unmapped_count = len(visible)
    for machine in inventory.machines:
        machine.unmapped_count = per_machine.get(machine.machine_id, 0)
    inventory.blocked_collection = blocked_collection(session, visible)
    # Credential sources a machine found but could not read (a locked or app-bound
    # browser cookie store). They are not "unmapped": there is no credential to map.
    inventory.blocked_sources = source_blocks(session, source_block_rows(session))
    from app.services.credential_file_migration import get_file_migration_notice

    inventory.credential_file_migration_notice = get_file_migration_notice(session)
    return inventory


@router.post("/credentials/migration-notice/dismiss")
@limiter.limit("30/minute")
async def dismiss_credential_file_migration(
    request: Request,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, str]:
    from app.services.credential_file_migration import dismiss_file_migration_notice

    dismiss_file_migration_notice(session)
    session.commit()
    return {"status": "ok"}


@router.post("/credentials/{provider}/{account_id}/{source_id}/refresh")
@limiter.limit("5/minute")
async def refresh_credential_source(
    request: Request,
    provider: str,
    account_id: str,
    source_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Refresh one credential source's OAuth token, writing the result back into that
    source's bundle (the one collectors read) and the merged account entry."""
    candidates = await token_cache.get_source_candidates(provider, account_id)
    bundle = next((c for c in candidates if c["source_id"] == source_id), None)
    if bundle is None:
        raise HTTPException(status_code=404, detail="No live credential for this source")
    tokens = bundle["tokens"]
    if not has_refresh_credential(tokens):
        raise HTTPException(status_code=400, detail="No refresh token available")

    from app.services.token_refresher import machine_owns_credential, refresh_oauth_token

    # Every candidate, not just this bundle: a pasted ``config:`` bundle can hold the same
    # refresh secret as a machine's, and refreshing it would rotate that CLI's token too.
    if machine_owns_credential(provider, tokens, candidates):
        raise HTTPException(status_code=409, detail=_RENEWED_BY_MACHINE)

    try:
        new_tokens = await refresh_oauth_token(provider, tokens)
        await token_cache.apply_refresh_to_sources(provider, account_id, tokens, new_tokens)
        if not bundle.get("identity_pending"):
            # ``store`` without a ``source_id`` updates only the merged account entry; the
            # source bundle itself was already written by ``apply_refresh_to_sources`` above.
            await token_cache.store(
                provider,
                new_tokens,
                account_id,
                account_label=bundle.get("account_label"),
                source=bundle.get("sidecar_id") or bundle.get("source"),
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(
            f"Source refresh failed for {scrub_log(provider)}/{scrub_log(account_id)}: {e}"
        )
        raise HTTPException(status_code=502, detail="Upstream token refresh failed")
    audit_log.record(
        session,
        request,
        action="credential.source_refresh",
        target_id=f"{provider}/{account_id}",
        payload={"source_id": source_id},
    )
    return {"status": "refreshed"}


_MANAGED_ELSEWHERE = (
    "This credential is managed outside the cache (Settings → Providers "
    "or the server environment); change it there."
)


async def _forget_source(
    session: Session, provider: str, account_id: str, source_id: str
) -> Literal["removed", "managed_elsewhere", "not_found"]:
    """Drop one machine-reported credential's live bundle and durable row."""
    row = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == provider,
            CredentialSource.account_id == canonical_account_id(account_id),
            CredentialSource.source_id == source_id,
        )
    ).first()
    managed_elsewhere = is_server_source_id(source_id) or source_id.startswith("config:")
    if managed_elsewhere or (row is not None and row.sidecar_id is None):
        return "managed_elsewhere"
    removed = await token_cache.remove_source(
        provider, account_id, source_id, retire_matching_oauth=True
    )
    if row is None and not removed:
        return "not_found"
    if row is not None:
        session.delete(row)
        session.commit()
    return "removed"


def _clear_rejection_if_account_empty(session: Session, provider: str, account_id: str) -> None:
    remaining = session.exec(
        select(CredentialSource.source_id).where(
            CredentialSource.provider_id == provider,
            CredentialSource.account_id == canonical_account_id(account_id),
        )
    ).first()
    if remaining is None:
        # Nothing of this account is left to be rejected; don't keep it flagged invalid.
        auth_failures.clear(provider, account_id)


@router.delete("/credentials/{provider}/{account_id}/{source_id}")
@limiter.limit("20/minute")
async def delete_credential_source(
    request: Request,
    provider: str,
    account_id: str,
    source_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Forget one machine-reported credential: drops its live bundle and durable row.

    It comes back on the machine's next report if the credential is still there. Config
    (Settings → Providers) and server (env/file) credentials are managed elsewhere → 409.
    """
    outcome = await _forget_source(session, provider, account_id, source_id)
    if outcome == "managed_elsewhere":
        raise HTTPException(status_code=409, detail=_MANAGED_ELSEWHERE)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Credential source not found")
    _clear_rejection_if_account_empty(session, provider, account_id)
    audit_log.record(
        session,
        request,
        action="credential.source_delete",
        target_id=f"{provider}/{account_id}",
        payload={"source_id": source_id},
    )
    return {"ok": True}


class _RemoveSourcesBody(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=50)


@router.post("/credentials/{provider}/{account_id}/remove")
@limiter.limit("10/minute")
async def remove_credential_sources(
    request: Request,
    provider: str,
    account_id: str,
    body: _RemoveSourcesBody,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Forget several machine-reported credentials of one account in one call.

    Same semantics as the single delete, but a managed (config/server) or unknown source is
    reported under ``skipped`` instead of failing the batch, and the per-item rate limit
    doesn't apply — an account with many dead rows would otherwise hit it.
    """
    removed: list[str] = []
    skipped: list[dict[str, str]] = []
    for source_id in dict.fromkeys(body.source_ids):
        outcome = await _forget_source(session, provider, account_id, source_id)
        if outcome == "removed":
            removed.append(source_id)
        else:
            skipped.append({"source_id": source_id, "reason": outcome})
    if removed:  # a batch that removed nothing must not touch the account's rejection flag
        _clear_rejection_if_account_empty(session, provider, account_id)
        audit_log.record(
            session,
            request,
            action="credential.sources_remove",
            target_id=f"{provider}/{account_id}",
            payload={"removed": removed, "skipped": skipped},
        )
    return {"removed": removed, "skipped": skipped}


# --- Webhook alert configuration ---


__all__ = [
    "_RemoveSourcesBody",
    "_clear_rejection_if_account_empty",
    "_forget_source",
    "delete_credential_source",
    "dismiss_credential_file_migration",
    "get_credential_inventory",
    "refresh_credential_source",
    "remove_credential_sources",
]
_RENEWED_BY_MACHINE = (
    "This credential belongs to a machine's CLI, which renews it. Refreshing it here would "
    "rotate the refresh token and sign that CLI out."
)
