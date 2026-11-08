"""Source identity, merge, and borrowing helpers for the token cache.

Split from ``token_cache`` so persistence and read orchestration can change
independently. No cache key or TTL behavior lives here.
"""

import logging
import math
from contextvars import ContextVar
from typing import Any

from app.core.utils import CREDENTIAL_VALUE_KEYS, IdentityExtractor, scrub_log
from app.services.account_identity import canonical_account_id
from app.services.refresh_policy import ROTATING_REFRESH_PROVIDERS, machine_owns_credential

logger = logging.getLogger(__name__)

active_source: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "active_credential_source", default=None
)

OAUTH_CREDENTIAL_KEYS = {
    "oauth_token",
    "refresh_token",
    "id_token",
    "access_token",
    "cli_access_token",
    "cli_expires_at",
    "cli_device_id",
    "expiry_date",
    "client_id",
    "oauth_account_uuid",
    "oauth_read_only",
    "xai_access",
    "xai_refresh",
}
CLI_FAMILY_KEYS = frozenset({"cli_access_token", "cli_expires_at", "cli_device_id"})


def credential_expiry(tokens: dict[str, str]) -> float | None:
    expiry = IdentityExtractor.exp_from_tokens(tokens)
    if expiry is not None or not tokens.get("cli_access_token"):
        return expiry
    try:
        cli_expiry = float(tokens["cli_expires_at"])
        # Non-finite expiry stays unknown, preserving the replacement recovery path.
        return cli_expiry if math.isfinite(cli_expiry) else None
    except (KeyError, ValueError, TypeError):
        return None


def merge_cli_family(
    target: dict[str, str], incoming: dict[str, str], *, prefer_incoming: bool
) -> None:
    """The device identity and expiry belong to the selected CLI bearer."""
    if incoming.get("cli_access_token") and prefer_incoming:
        for key in CLI_FAMILY_KEYS:
            target.pop(key, None)
        target.update({key: value for key, value in incoming.items() if key in CLI_FAMILY_KEYS})


AUTH_VALUE_KEYS = CREDENTIAL_VALUE_KEYS
OAUTH_TOKEN_VALUE_KEYS = AUTH_VALUE_KEYS - {"api_key", "session_cookie"}

# Origins the user typed into the dashboard. They outrank every other origin
# (sidecar ids, "server") when a later push re-stamps `source`: a sidecar
# pushing *any* credential for the same account must not reclassify an
# explicit paste, or collectors read `input_source=sidecar` for the pasted
# key and a 401 on it stops being treated as authoritative (PR #352 review).
_EXPLICIT_SOURCES = frozenset({"config", "manual_config"})


def resolve_source(incoming: str | None, existing: str | None) -> str | None:
    """Pick the winning origin for an entry being (re)stored.

    Falsy incoming keeps the previous origin; an explicit dashboard origin is
    never downgraded by a sidecar/server push; otherwise the later push wins.
    """
    if not incoming:
        return existing
    if not existing:
        return incoming
    if existing in _EXPLICIT_SOURCES and incoming not in _EXPLICIT_SOURCES:
        return existing
    return incoming


class SourceCacheInstance:
    """An opaque, process-local identity retaining one live cache entry."""

    __slots__ = ("_entry",)

    def __init__(self, entry: object):
        self._entry = entry

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SourceCacheInstance) and self._entry is other._entry

    def __hash__(self) -> int:
        return id(self._entry)

    def __repr__(self) -> str:
        return "<SourceCacheInstance>"


class _TokenCacheRef:
    """Forward to ``app.services.token_cache.token_cache`` on each use.

    ``server_may_refresh`` is defined in this module and calls the singleton by
    the name ``token_cache``. Callers and tests replace that singleton on
    ``app.services.token_cache``, so this object resolves there instead of
    capturing a second instance.
    """

    def __getattr__(self, name: str) -> Any:
        from app.services import token_cache as token_cache_module

        return getattr(token_cache_module.token_cache, name)


token_cache = _TokenCacheRef()


def is_foreign_account_entry(entry_account_id: str, wanted_account_id: str | None) -> bool:
    """True when a cache entry is keyed to a *different, identified* account.

    Identity-mismatch fallbacks (a collector searching every cached entry
    for a usable token) may only borrow entries that carry no identity of
    their own — ``"default"`` or an opaque credential hash — or the wanted
    account itself. An entry keyed by another email belongs to that
    account; using it would report account B's quota on account A's card.
    """
    entry = canonical_account_id(entry_account_id)
    if wanted_account_id and entry == canonical_account_id(wanted_account_id):
        return False
    return "@" in entry


def borrowable_entries(
    entries: list[dict[str, Any]], wanted_account_id: str | None, *, provider: str = ""
) -> list[dict[str, Any]]:
    """Filter ``get_accounts`` rows down to those a fallback may borrow.

    Drops entries keyed to another identified account
    (:func:`is_foreign_account_entry`). One exception keeps the identity
    bootstrap working: an *unpinned* collector (``wanted_account_id`` is
    None, e.g. a fresh server with no ``LatestUsage`` row yet) may borrow
    when the cache holds exactly one identified account — a single-account
    deployment has no cross-account risk, and that first card is what pins
    the collector to its identity. When everything is excluded the outage
    is logged instead of going silent.
    """
    allowed = [
        a for a in entries if not is_foreign_account_entry(a["account_id"], wanted_account_id)
    ]
    if allowed or not entries:
        return allowed
    identities = {canonical_account_id(a["account_id"]) for a in entries}
    if not wanted_account_id and len(identities) == 1:
        return entries
    logger.warning(
        "%s: %d cached credential(s) belong to other accounts; not borrowing any for %s",
        scrub_log(provider or "token_cache"),
        len(entries),
        "the unpinned collector" if not wanted_account_id else "this account",
    )
    return []


async def server_may_refresh(
    provider: str,
    account_id: str,
    tokens: dict[str, str],
    *,
    merged_source: str | None = None,
) -> bool:
    """Whether the server may exchange this credential's refresh token.

    False for a rotating provider's login that a machine's CLI owns (see
    ``refresh_policy.machine_owns_credential``): refreshing it signs that CLI out.
    """
    if provider not in ROTATING_REFRESH_PROVIDERS:
        return True
    bundles = await token_cache.get_source_candidates(provider, account_id)
    return not machine_owns_credential(provider, tokens, bundles, merged_source=merged_source)
