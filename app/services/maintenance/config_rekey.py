"""Move a provider's configuration and derived state from one account_id to
another — the Data Health `config_default_keyed` fixer.

Targets the shape found repeatedly in production: a provider_configs row
still keyed `account_id="default"` whose `account_label` is already a real
identity (an email) — the credential works, but every write path that keys
on `(provider_id, account_id)` (most importantly `POST
/fleet/events/pending/assign`, which 404s unless a ProviderConfig row exists
for the target account — see app/api/endpoints/fleet.py) can't address the
real account until the row's own key catches up with its label.

Follows the collision-handling pattern in
`app/services/account_canonicalization.py`: bulk `UPDATE OR IGNORE` +
leftover-row check, rather than a generic merge — provider_configs holds
operator intent (credentials) and is never silently merged field-by-field.

`apply_rekey_config` only touches the database; the caller (a Data Health
job, which owns the event loop) must run the returned list of async hooks
afterward — mirroring the token_cache move + collector re-sync
`app/api/endpoints/system.py`'s provider-config PUT/DELETE handlers already
do after their own commits.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from sqlmodel import Session, select

from app.models.db import ProviderConfig
from app.services.credential_tags import CredentialTagRepo
from app.services.maintenance.account_merge import (
    MergePlan,
    merge_gauge_series,
    plan_merge_gauge_series,
)

logger = logging.getLogger(__name__)

AsyncHook = Callable[[], Awaitable[None]]
OnCollision = Literal["abort", "archive_default"]


class RekeyCollisionError(ValueError):
    """Raised when `on_collision="abort"` and a ProviderConfig already
    exists at the target account_id."""


@dataclass
class RekeyPlan:
    provider_config_exists_at_target: bool = False
    credential_tags: int = 0
    webhook_configs: int = 0
    webhook_configs_dropped_duplicate: int = 0
    gauge_series: MergePlan = field(default_factory=MergePlan)


@dataclass
class RekeyResult:
    provider_config_moved: bool = False
    provider_config_archived_source: bool = False
    credential_tags_moved: int = 0
    webhook_configs_moved: int = 0
    webhook_configs_dropped_duplicate: int = 0
    gauge_series: MergePlan = field(default_factory=MergePlan)


def _get_config(session: Session, provider_id: str, account_id: str) -> ProviderConfig | None:
    return session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == provider_id, ProviderConfig.account_id == account_id
        )
    ).first()


def plan_rekey_config(
    session: Session, *, provider_id: str, old_account_id: str, new_account_id: str
) -> RekeyPlan:
    """Read-only preview."""
    target_exists = _get_config(session, provider_id, new_account_id) is not None
    tags = CredentialTagRepo.list_by_provider(session, provider_id=provider_id)
    tags_at_old = sum(1 for t in tags if t.account_id == old_account_id)

    from app.models.db import WebhookConfig

    webhooks = session.exec(
        select(WebhookConfig).where(
            WebhookConfig.provider_id == provider_id, WebhookConfig.account_id == old_account_id
        )
    ).all()
    existing_urls = {
        w.url
        for w in session.exec(
            select(WebhookConfig).where(
                WebhookConfig.provider_id == provider_id, WebhookConfig.account_id == new_account_id
            )
        ).all()
    }
    dupes = sum(1 for w in webhooks if w.url in existing_urls)

    gauge_plan = plan_merge_gauge_series(
        session, provider_id=provider_id, source=old_account_id, target=new_account_id
    )

    return RekeyPlan(
        provider_config_exists_at_target=target_exists,
        credential_tags=tags_at_old,
        webhook_configs=len(webhooks) - dupes,
        webhook_configs_dropped_duplicate=dupes,
        gauge_series=gauge_plan,
    )


def apply_rekey_config(
    session: Session,
    *,
    provider_id: str,
    old_account_id: str,
    new_account_id: str,
    on_collision: OnCollision = "abort",
) -> tuple[RekeyResult, list[AsyncHook]]:
    """Move provider_configs / credential_tags / webhook_configs / gauge
    series from `old_account_id` to `new_account_id`. Commits.

    Returns `(result, hooks)` — `hooks` are async callables the caller must
    await afterward (the token_cache move and a collector re-sync), since
    this function only touches the database. Raises `RekeyCollisionError`
    if a ProviderConfig already exists at `new_account_id` and
    `on_collision="abort"` (the default) — nothing is written in that case.
    """
    source_config = _get_config(session, provider_id, old_account_id)
    target_config = _get_config(session, provider_id, new_account_id)

    if source_config is None:
        raise ValueError(f"No provider_config at {provider_id}/{old_account_id} to rekey")
    if target_config is not None and on_collision == "abort":
        raise RekeyCollisionError(
            f"provider_config already exists at {provider_id}/{new_account_id} "
            "(pass on_collision='archive_default' to archive the old row instead)"
        )

    result = RekeyResult()

    if target_config is None:
        source_config.account_id = new_account_id
        session.add(source_config)
        result.provider_config_moved = True
    else:
        # on_collision == "archive_default": the target already has its own
        # working config — the stale default-keyed row can't be merged
        # field-by-field (it may carry different credentials), so archive
        # it in place rather than lose it silently.
        source_config.archived = True
        source_config.enabled = False
        session.add(source_config)
        result.provider_config_archived_source = True

    session.commit()

    # credential_tags.account_id has no uniqueness of its own (the table's
    # keys are (provider_id, credential_origin[, sidecar_id])), so this is a
    # plain bulk retag — no collision handling needed.
    tags = CredentialTagRepo.list_by_provider(session, provider_id=provider_id)
    for tag in tags:
        if tag.account_id == old_account_id:
            tag.account_id = new_account_id
            session.add(tag)
            result.credential_tags_moved += 1
    session.commit()

    from app.models.db import WebhookConfig

    webhooks = session.exec(
        select(WebhookConfig).where(
            WebhookConfig.provider_id == provider_id, WebhookConfig.account_id == old_account_id
        )
    ).all()
    existing_urls = {
        w.url
        for w in session.exec(
            select(WebhookConfig).where(
                WebhookConfig.provider_id == provider_id, WebhookConfig.account_id == new_account_id
            )
        ).all()
    }
    for webhook in webhooks:
        if webhook.url in existing_urls:
            # Same (provider_id, url) already alerts for the target account
            # — keep that one, drop the now-redundant duplicate.
            session.delete(webhook)
            result.webhook_configs_dropped_duplicate += 1
        else:
            webhook.account_id = new_account_id
            session.add(webhook)
            result.webhook_configs_moved += 1
    session.commit()

    result.gauge_series = merge_gauge_series(
        session, provider_id=provider_id, source=old_account_id, target=new_account_id
    )

    hooks: list[AsyncHook] = [
        _make_token_cache_move_hook(
            provider_id, old_account_id, new_account_id, archive_source=target_config is not None
        )
    ]
    return result, hooks


def _make_token_cache_move_hook(
    provider_id: str, old_account_id: str, new_account_id: str, *, archive_source: bool = False
) -> AsyncHook:
    async def _move() -> None:
        from app.services.token_cache import token_cache

        existing = await token_cache.get_with_metadata(provider_id, old_account_id)
        if existing is not None and not archive_source:
            tokens, metadata = existing
            await token_cache.store(
                provider_id,
                tokens,
                account_id=new_account_id,
                account_label=metadata.get("account_label"),
                source=metadata.get("source"),
            )
            await token_cache.remove(provider_id, old_account_id)
        elif archive_source:
            # The target has its own identity and cache; never overwrite it
            # with the default-keyed source credentials when archiving.
            await token_cache.remove(provider_id, old_account_id)

        from app.services import auth_failures

        auth_failures.clear(provider_id, old_account_id)
        auth_failures.clear(provider_id, new_account_id)

        from app.core.cache import cache_clear

        cache_clear()

        try:
            from app.services.collector_manager import manager

            await manager._sync_collectors(force=True)
        except Exception as exc:  # noqa: BLE001 — mutation already succeeded
            logger.warning(
                "Failed to trigger collector sync after rekeying %s/%s -> %s: %s",
                provider_id,
                old_account_id,
                new_account_id,
                exc,
            )

    return _move
