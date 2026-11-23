"""Derive why a stale usage card is stale at serve time.

Never persisted on ``LatestUsage`` or ``LatestUsageContribution``; computed
fresh when ``usage_overview`` serves cards so credential additions, removals,
or health changes take effect immediately.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlmodel import Session, col, select

from app.models.db import CredentialSource, ProviderConfig, SidecarSourceContribution
from app.services.account_identity import canonical_account_id
from app.services.credentials.sources import effective_health
from app.services.token_cache import TokenCache, token_cache

STALE_REASON_NO_CREDENTIAL = "no_credential"
STALE_REASON_CREDENTIAL_FAILING = "credential_failing"
_UNHEALTHY_SOURCE_HEALTHS = frozenset({"auth_failed", "unavailable"})


def derive_stale_reason(
    session: Session,
    provider_id: str,
    account_id: str | None,
    *,
    cache: TokenCache | None = None,
) -> str | None:
    """Return ``no_credential``, ``credential_failing``, or ``None`` for a stale card."""
    canon_aid = canonical_account_id(account_id)

    config = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == provider_id,
            ProviderConfig.account_id == canon_aid,
        )
    ).first()
    if config is None and canon_aid != "default":
        config = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == provider_id,
                ProviderConfig.account_id == "default",
            )
        ).first()
    if config is not None and (not config.enabled or config.archived):
        return None

    sources = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == provider_id,
            CredentialSource.account_id == canon_aid,
            col(CredentialSource.enabled).is_(True),
        )
    ).all()
    if sources:
        if all(effective_health(s) in _UNHEALTHY_SOURCE_HEALTHS for s in sources):
            return STALE_REASON_CREDENTIAL_FAILING
        return None

    if config is None:
        return None

    if (
        config.api_key_encrypted
        or config.session_cookie_encrypted
        or config.oai_sc_cookie_encrypted
    ):
        return None

    has_contribution = (
        session.exec(
            select(SidecarSourceContribution.id).where(
                SidecarSourceContribution.provider_id == provider_id,
                SidecarSourceContribution.account_id == canon_aid,
                col(SidecarSourceContribution.payload_encrypted).is_not(None),
                SidecarSourceContribution.payload_encrypted != "",
            )
        ).first()
        is not None
    )
    if has_contribution:
        return None

    active_cache = cache if cache is not None else token_cache
    if active_cache.has_account_entry(provider_id, canon_aid):
        return None

    return STALE_REASON_NO_CREDENTIAL


def apply_stale_reasons(
    session: Session,
    cards: Iterable[dict[str, Any]],
    *,
    cache: TokenCache | None = None,
) -> None:
    """Populate or clear ``stale_reason`` across cards, deriving once per (provider, account)."""
    memo: dict[tuple[str, str], str | None] = {}
    for card in cards:
        if not (card.get("stale") is True or card.get("collection_failing") is True):
            card.pop("stale_reason", None)
            continue
        provider_id = card.get("provider_id")
        if not provider_id:
            card.pop("stale_reason", None)
            continue
        key = (provider_id, canonical_account_id(card.get("account_id")))
        if key not in memo:
            memo[key] = derive_stale_reason(session, provider_id, key[1], cache=cache)
        reason = memo[key]
        if reason is not None:
            card["stale_reason"] = reason
        else:
            card.pop("stale_reason", None)


def apply_stale_reason(
    session: Session,
    card: dict[str, Any],
    *,
    cache: TokenCache | None = None,
) -> None:
    """Populate or clear ``stale_reason`` on a card dict in place."""
    apply_stale_reasons(session, [card], cache=cache)
