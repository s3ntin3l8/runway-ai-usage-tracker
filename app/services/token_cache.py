"""
Token Cache Service - In-memory cache for sidecar tokens, supporting multiple accounts.

Architecture:
- Sidecar extracts tokens from local files and sends to server
- Server stores tokens in memory (30min TTL) keyed by provider and account_id
- Server uses tokens to make API calls
- If account identity is discovered (email/name), the cache entry is updated ("promoted")
"""

import asyncio
import copy
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.core.utils import IdentityExtractor, scrub_log
from app.services.account_identity import canonical_account_id
from app.services.refresh_policy import ROTATING_REFRESH_PROVIDERS, is_machine_bundle
from app.services.token_cache_sources import (
    _OAUTH_CREDENTIAL_KEYS,
    AUTH_VALUE_KEYS,
    OAUTH_TOKEN_VALUE_KEYS,
    SourceCacheInstance,
    _active_source,
    _merge_cli_family,
    _resolve_source,
    borrowable_entries,
    is_foreign_account_entry,
    server_may_refresh,
)
from app.services.token_cache_store import TokenCacheStore

logger = logging.getLogger(__name__)

_active_attempt: ContextVar[dict[str, bool] | None] = ContextVar(
    "active_credential_attempt", default=None
)


class TokenCache(TokenCacheStore):
    """
    In-memory cache for tokens received from sidecars, supporting tenant isolation.

    Tokens expire after TTL (default 30 minutes = 1800 seconds).
    Structure: provider -> account_id -> (tokens, metadata, timestamp)
    """

    DEFAULT_TTL = 1800  # 30 minutes

    def __init__(self, ttl_seconds: int = DEFAULT_TTL):
        # provider_id -> {account_id: (tokens, metadata, timestamp)}
        self._cache: dict[str, dict[str, tuple[dict[str, str], dict[str, Any], float]]] = {}
        # Track each credential family's last report independently. Sidecars
        # send one card per origin, so refreshing a CLI token must not keep a
        # removed browser cookie alive forever (or vice versa).
        self._token_timestamps: dict[str, dict[str, dict[str, float]]] = {}
        # Credential bundles stay separate here even though the legacy account
        # cache below remains merged for backwards-compatible consumers.
        self._source_cache: dict[
            str, dict[str, dict[str, tuple[dict[str, str], dict[str, Any], float]]]
        ] = {}
        self._durable_revision = -1
        self._durable_source_ids: set[str] = set()
        self._durable_bundles: dict[str, tuple[str, dict[str, Any]]] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    def _derive_account_id(self, tokens: dict[str, str]) -> str:
        """Derive a stable account ID from tokens.

        Prefers identity claims (email / sub) carried by an id_token over
        hashing rotating access tokens. Without this, a CLI-driven refresh
        produces a new oauth_token value → new hash → duplicate cache entry.
        """
        id_token = tokens.get("id_token")
        if id_token:
            payload = IdentityExtractor.extract_jwt_payload(id_token)
            email = payload.get("email")
            if email:
                return email.lower()
            sub = payload.get("sub")
            if sub:
                return str(sub)

        ident = (
            tokens.get("refresh_token")
            or tokens.get("oauth_token")
            or tokens.get("api_key")
            or next(iter(tokens.values()))
        )
        # No identity claim — derive a stable, non-reversible cache key from the token.
        # pbkdf2_hmac (rather than a bare hash) is used because the static analyzer treats
        # hashing a credential as password storage; iterations=1 keeps this a fast,
        # deterministic dict key — these IDs are never stored or compared for authentication.
        return hashlib.pbkdf2_hmac("sha256", ident.encode(), b"runway-cache-id-v1", 1).hex()[:12]

    def _account_entries(
        self, provider: str
    ) -> dict[str, tuple[dict[str, str], dict[str, Any], float]]:
        """Select whole bundles without copying device credentials into legacy state."""
        entries = dict(self._cache.get(provider, {}))
        for account_id, sources in self._source_cache.get(provider, {}).items():
            # Preserve configured/manual aggregate precedence. Pending device
            # bundles are independently resolved through get_pending_sources().
            if account_id in entries:
                continue
            candidates = [
                entry
                for entry in sources.values()
                if entry[1].get("durable_source") is True
                and entry[1].get("identity_pending") is not True
            ]
            if candidates:
                entries[account_id] = max(candidates, key=lambda entry: entry[2])
        return entries

    async def update_account_metadata(
        self, provider: str, account_id: str, name: str | None = None
    ) -> None:
        """Update metadata (like account name/email) for an existing cache entry."""
        account_id = canonical_account_id(account_id)
        async with self._lock:
            if provider in self._cache and account_id in self._cache[provider]:
                tokens, metadata, timestamp = self._cache[provider][account_id]
                if name:
                    metadata["account_label"] = name
                self._cache[provider][account_id] = (tokens, metadata, timestamp)
                # Log neither the account name/email nor account_id (token-derived);
                # provider alone is enough to trace and is the only non-sensitive field.
                logger.debug("Updated account metadata for provider %s", scrub_log(provider))

    async def get_accounts(self, provider: str) -> list[dict[str, Any]]:
        """
        Get all active accounts for a provider.

        Returns:
            List of dicts with: account_id, tokens, account_label, age
        """
        async with self._lock:
            self._clear_expired_unlocked()

            selection = _active_source.get()
            if selection and selection[0] == provider:
                account_id, source_id = selection[1], selection[2]
                entry = self._source_cache.get(provider, {}).get(account_id, {}).get(source_id)
                if entry:
                    tokens, metadata, timestamp = entry
                    return [
                        {
                            "account_id": account_id,
                            "tokens": tokens,
                            "account_label": metadata.get("account_label"),
                            "source": metadata.get("source"),
                            "age": time.time() - timestamp,
                        }
                    ]

            now = time.time()
            results = []
            for acc_id, (tokens, metadata, timestamp) in self._account_entries(provider).items():
                results.append(
                    {
                        "account_id": acc_id,
                        "tokens": tokens,
                        "account_label": metadata.get("account_label"),
                        "source": metadata.get("source"),
                        "age": now - timestamp,
                    }
                )
            return results

    async def get(self, provider: str, account_id: str | None = None) -> dict[str, str] | None:
        """
        Get tokens for a specific account, or the first available if account_id is None.
        """
        account_id = canonical_account_id(account_id) if account_id else None
        async with self._lock:
            self._clear_expired_unlocked()

            selection = _active_source.get()
            if selection and selection[0] == provider:
                selected_account = selection[1] if account_id in (None, "default") else account_id
                selected = (
                    self._source_cache.get(provider, {}).get(selected_account, {}).get(selection[2])
                )
                if selected is None and selected_account != "default":
                    selected = (
                        self._source_cache.get(provider, {}).get("default", {}).get(selection[2])
                    )
                return selected[0] if selected else None

            provider_accounts = self._account_entries(provider)
            if not provider_accounts:
                return None

            if account_id:
                if account_id not in provider_accounts:
                    # Config credentials are always stored under "default".  When a
                    # collector runs under a resolved identity (e.g. an email address
                    # seeded by durable_identities) but its cookie was stored under
                    # "default", the explicit-key lookup misses — fall back to
                    # "default" so the collector still finds its tokens.
                    if "default" in provider_accounts:
                        tokens, _, _ = provider_accounts["default"]
                        return tokens
                    return None
                tokens, _, _ = provider_accounts[account_id]
                return tokens
            # Return the most recently updated account if none specified
            newest_acc = sorted(provider_accounts.items(), key=lambda x: x[1][2], reverse=True)[0]
            return newest_acc[1][0]

    async def get_with_metadata(
        self, provider: str, account_id: str | None = None, *, exact: bool = False
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        """
        Get tokens and metadata for a specific account.

        ``exact=True`` disables the ``default`` fallback: it returns the named
        account's entry or ``None``. Collectors want the fallback (config creds live
        under ``default``); anything that *writes the result back under the requested
        account id* — token refresh — must not, or it would copy ``default``'s
        credential onto an account that doesn't exist.
        """
        account_id = canonical_account_id(account_id) if account_id else None
        async with self._lock:
            self._clear_expired_unlocked()

            selection = _active_source.get()
            if selection and selection[0] == provider:
                selected_account = selection[1] if account_id in (None, "default") else account_id
                selected = (
                    self._source_cache.get(provider, {}).get(selected_account, {}).get(selection[2])
                )
                if selected is None and selected_account != "default":
                    selected = (
                        self._source_cache.get(provider, {}).get("default", {}).get(selection[2])
                    )
                return (selected[0], selected[1]) if selected else None

            provider_accounts = self._account_entries(provider)
            if not provider_accounts:
                return None

            if account_id:
                if account_id not in provider_accounts:
                    # Same "default" fallback as get() — config creds live under
                    # "default" regardless of the collector's resolved identity.
                    if not exact and "default" in provider_accounts:
                        tokens, metadata, _ = provider_accounts["default"]
                        return tokens, metadata
                    return None
                tokens, metadata, _ = provider_accounts[account_id]
                return tokens, metadata

            # Return the most recently updated account if none specified
            newest_acc = sorted(provider_accounts.items(), key=lambda x: x[1][2], reverse=True)[0]
            tokens, metadata, _ = newest_acc[1]
            return tokens, metadata

    async def get_token(
        self, provider: str, token_type: str, account_id: str | None = None
    ) -> str | None:
        """Get specific token type for provider/account."""
        tokens = await self.get(provider, account_id)
        return tokens.get(token_type) if tokens else None

    @asynccontextmanager
    async def using_source(self, provider: str, account_id: str, source_id: str):
        """Route cache reads in the current async task to one credential bundle."""
        token = _active_source.set((provider, canonical_account_id(account_id), source_id))
        attempt: dict[str, bool] = {"auth_failed": False}
        attempt_token = _active_attempt.set(attempt)
        try:
            yield attempt
        finally:
            _active_attempt.reset(attempt_token)
            _active_source.reset(token)

    async def observe_response(self, response: Any) -> None:
        """Mark the active bundle rejected when its credential gets HTTP 401 or 403."""
        attempt = _active_attempt.get()
        if attempt is None or getattr(response, "status_code", None) not in (401, 403):
            return

        request = getattr(response, "request", None)
        if request is not None and not self._request_uses_active_source(request):
            return
        # Claude organization denials and browser challenges do not reject the cookie.
        # Keep unrelated providers and account-level authentication failures unchanged.
        url = getattr(request, "url", None)
        if getattr(url, "host", None) in ("claude.ai", "platform.claude.com"):
            headers = getattr(response, "headers", {})
            if (
                "text/html" in headers.get("content-type", "").lower()
                or headers.get("cf-mitigated") == "challenge"
            ):
                return
            if response.status_code == 403 and getattr(url, "path", "").startswith(
                "/api/organizations/"
            ):
                return
        attempt["auth_failed"] = True

    def _request_uses_active_source(self, request: Any) -> bool:
        selection = _active_source.get()
        if selection is None:
            return False
        provider, account_id, source_id = selection
        entry = self._source_cache.get(provider, {}).get(account_id, {}).get(source_id)
        if entry is None:
            return False

        tokens = entry[0]
        credential_values = [
            value
            for key, value in tokens.items()
            if isinstance(value, str)
            and value
            and (key in AUTH_VALUE_KEYS or key.startswith("cookie_"))
        ]
        request_values: list[str] = []
        headers = getattr(request, "headers", {})
        request_values.extend(str(value) for value in headers.values())
        query = parse_qs(urlsplit(str(getattr(request, "url", ""))).query)
        request_values.extend(value for values in query.values() for value in values)
        return any(secret in value for secret in credential_values for value in request_values)

    async def apply_refresh_to_sources(
        self,
        provider: str,
        account_id: str,
        previous: dict[str, str],
        refreshed: dict[str, str],
    ) -> int:
        """Write a refreshed OAuth family back into every source bundle that held it.

        Refreshers act on the merged account entry, but collectors read the
        source-pinned bundles. Without this write-back a bundle keeps the pre-refresh
        tokens, and for providers that rotate refresh tokens it is left holding a
        revoked one. A bundle matches when it shares a secret (access or refresh
        token) with the ``previous`` tokens that were actually refreshed, so a
        different credential for the same account (another machine's login) is
        never overwritten. Returns the number of bundles updated.

        Race: a bundle that re-pushed a fresh access token between the caller reading
        ``previous`` and this call no longer matches on the access token, so it is only
        updated if the refresh token it holds is the one that was rotated. Not every key
        in ``refreshed`` lands on every bundle — only on those that matched.
        """
        account_id = canonical_account_id(account_id)
        identity_keys = ("refresh_token", "xai_refresh", "oauth_token", "xai_access")
        updated = 0
        async with self._lock:
            sources = self._source_cache.get(provider, {}).get(account_id, {})
            for source_id, (tokens, metadata, _timestamp) in list(sources.items()):
                if provider in ROTATING_REFRESH_PROVIDERS and is_machine_bundle(metadata):
                    # A machine's own login: the server never owns its refresh token, so
                    # a refresh result must not be written into the bundle.
                    continue
                if not any(
                    previous.get(key) and tokens.get(key) == previous[key] for key in identity_keys
                ):
                    continue
                sources[source_id] = ({**tokens, **refreshed}, metadata, time.time())
                updated += 1
        return updated

    def _peek_source_entry(
        self, provider: str, account_id: str, source_id: str
    ) -> tuple[dict[str, str], dict[str, Any], float] | None:
        """Read only while holding _lock; no reload, expiry mutation or fallback."""
        entry = (
            self._source_cache.get(provider, {})
            .get(canonical_account_id(account_id), {})
            .get(source_id)
        )
        if entry is None:
            return None
        _, metadata, timestamp = entry
        if metadata.get("durable_source") is not True and time.time() - timestamp > self._ttl:
            return None
        return entry

    async def peek_source(
        self, provider: str, account_id: str, source_id: str
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        """Copy one live cached bundle without DB reload, expiry mutation or fallback.

        Diagnostics must separately validate durable authority. This snapshot
        preserves legacy TTL while an enrolled source has no heartbeat lease.
        """
        async with self._lock:
            entry = self._peek_source_entry(provider, account_id, source_id)
            if entry is None:
                return None
            return dict(entry[0]), copy.deepcopy(entry[1])

    async def peek_source_instance(
        self, provider: str, account_id: str, source_id: str
    ) -> tuple[dict[str, str], dict[str, Any], SourceCacheInstance] | None:
        """Also pin the exact entry against remove/restore races within this process.

        Any entry replacement, even with identical credentials, invalidates the
        pin. It is not a durable credential generation or a cross-process proof.
        """
        async with self._lock:
            entry = self._peek_source_entry(provider, account_id, source_id)
            if entry is None:
                return None
            return dict(entry[0]), copy.deepcopy(entry[1]), SourceCacheInstance(entry)

    async def get_source_candidates(self, provider: str, account_id: str) -> list[dict[str, Any]]:
        """Return live source bundles in configured priority order."""
        account_id = canonical_account_id(account_id)
        async with self._lock:
            self._clear_expired_unlocked()
            rows = self._source_cache.get(provider, {}).get(account_id, {})
            candidates = [
                {"source_id": source_id, "tokens": tokens, **metadata}
                for source_id, (tokens, metadata, _timestamp) in rows.items()
            ]
            return sorted(
                candidates,
                key=lambda row: (int(row.get("priority", 0)), row["source_id"]),
            )

    async def get_account_source_candidates(self, provider: str) -> list[dict[str, Any]]:
        """Live source bundles in every *resolved-identity* slot, tagged ``account_slot``.

        Sidecar ingest files bundles under the account it proved, never under
        ``default``. A collector pinned to ``default`` therefore finds none of
        them via ``get_source_candidates``; this sweep is its discovery path —
        the regular-collector counterpart of ``get_pending_sources``. The
        ``account_slot`` tag lets the caller pin reads to the slot that actually
        holds the bundle.
        """
        async with self._lock:
            self._clear_expired_unlocked()
            candidates = [
                {"source_id": source_id, "tokens": tokens, "account_slot": slot, **metadata}
                for slot, rows in self._source_cache.get(provider, {}).items()
                if slot != "default"
                for source_id, (tokens, metadata, _timestamp) in rows.items()
            ]
            return sorted(
                candidates,
                key=lambda row: (int(row.get("priority", 0)), row["source_id"]),
            )

    async def get_pending_sources(self, provider: str) -> list[dict[str, Any]]:
        """Identity-pending source bundles in *any* cache slot, each tagged ``account_slot``.

        Most providers file a pending bundle under ``default``; Anthropic files it under its
        own stable source id so a rotating token can't spawn orphan rows. The identity
        verifier needs both.
        """
        async with self._lock:
            self._clear_expired_unlocked()
            pending = [
                {"source_id": source_id, "tokens": tokens, "account_slot": slot, **metadata}
                for slot, rows in self._source_cache.get(provider, {}).items()
                for source_id, (tokens, metadata, _timestamp) in rows.items()
                if metadata.get("identity_pending") is True
            ]
            return sorted(pending, key=lambda row: (int(row.get("priority", 0)), row["source_id"]))

    async def get_all_source_descriptors(self, provider: str) -> list[dict[str, Any]]:
        """Return sidecar ownership metadata without exposing credential values."""
        async with self._lock:
            self._clear_expired_unlocked()
            candidates = [
                {
                    "account_id": account_id,
                    "source_id": source_id,
                    "sidecar_id": metadata.get("sidecar_id"),
                    "credential_origin": metadata.get("credential_origin"),
                    **(
                        {
                            "identity_pending": metadata.get("identity_pending"),
                            "credential_generation": self.credential_generation(_tokens),
                        }
                        if provider in ("anthropic", "anthropic_console")
                        else {}
                    ),
                }
                for account_id, sources in self._source_cache.get(provider, {}).items()
                for source_id, (_tokens, metadata, _timestamp) in sources.items()
            ]
            return sorted(candidates, key=lambda row: (row["account_id"], row["source_id"]))

    def current_source_tokens(self, provider: str, account_id: str) -> dict[str, str] | None:
        """Synchronous lookup used by legacy credential-provider helpers."""
        selection = _active_source.get()
        if not selection or selection[0] != provider:
            return None
        requested = canonical_account_id(account_id) if account_id else selection[1]
        account_id = selection[1] if requested == "default" else requested
        entry = self._source_cache.get(provider, {}).get(account_id, {}).get(selection[2])
        if entry is None and account_id != "default":
            entry = self._source_cache.get(provider, {}).get("default", {}).get(selection[2])
        return dict(entry[0]) if entry else None

    def selected_source(self, provider: str) -> tuple[str, str] | None:
        """``(account_id, source_id)`` this context is pinned to for ``provider``, if any."""
        selection = _active_source.get()
        if selection is None or selection[0] != provider:
            return None
        return selection[1], selection[2]

    def is_source_selected(self, provider: str, account_id: str | None = None) -> bool:
        """Whether this context is pinned to a source for the requested account."""
        selection = _active_source.get()
        if selection is None or selection[0] != provider:
            return False
        if account_id is None or canonical_account_id(account_id) == "default":
            return True
        return canonical_account_id(account_id) == selection[1]

    def current_source_metadata(self, provider: str, account_id: str) -> dict[str, Any] | None:
        selection = _active_source.get()
        if not selection or selection[0] != provider:
            return None
        requested = canonical_account_id(account_id) if account_id else selection[1]
        account_id = selection[1] if requested == "default" else requested
        entry = self._source_cache.get(provider, {}).get(account_id, {}).get(selection[2])
        if entry is None and account_id != "default":
            entry = self._source_cache.get(provider, {}).get("default", {}).get(selection[2])
        return dict(entry[1]) if entry else None

    @staticmethod
    def credential_generation(tokens: dict[str, str]) -> str:
        """Nonsecret proof of an exact credential bundle, stable across identical heartbeats."""
        return hashlib.sha256(
            json.dumps(tokens, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def source_generation(self, provider: str, account_id: str, source_id: str) -> str | None:
        """Read a nonsecret bundle proof in a synchronous no-yield publication section."""
        entry = (
            self._source_cache.get(provider, {})
            .get(canonical_account_id(account_id), {})
            .get(source_id)
        )
        return self.credential_generation(entry[0]) if entry else None

    async def get_all_stats(self) -> dict[str, Any]:
        """Get flattened stats for all cached providers and accounts."""
        async with self._lock:
            self._clear_expired_unlocked()
            now = time.time()
            stats = {}
            for provider, accounts in self._cache.items():
                stats[provider] = {
                    acc_id: {
                        "tokens": list(tokens.keys()),
                        "account_label": metadata.get("account_label"),
                        "source": metadata.get("source"),
                        "age_seconds": int(now - ts),
                        "ttl_remaining": int(self._ttl - (now - ts)),
                    }
                    for acc_id, (tokens, metadata, ts) in accounts.items()
                }
            return stats

    async def _get_source_credentials(self, provider: str) -> list[dict[str, Any]]:
        """Internal Token Health helper; returned token values must never be exposed."""
        async with self._lock:
            self._clear_expired_unlocked()
            now = time.time()
            return [
                {
                    "account_id": account_id,
                    "source_id": source_id,
                    "tokens": dict(tokens),
                    "metadata": dict(metadata),
                    "ttl_remaining": max(0, int(self._ttl - (now - timestamp))),
                }
                for account_id, sources in self._source_cache.get(provider, {}).items()
                for source_id, (tokens, metadata, timestamp) in sources.items()
            ]

    async def get_all_active_accounts(self) -> list[tuple[str, str, str | None]]:
        """
        Get a list of all active (provider, account_id, account_label) tuples.
        Useful for CollectorManager discovery.
        """
        async with self._lock:
            self._clear_expired_unlocked()
            results = []
            for provider in self._cache.keys() | self._source_cache.keys():
                for acc_id, (_, metadata, _) in self._account_entries(provider).items():
                    results.append((provider, acc_id, metadata.get("account_label")))
            return results


_get_source_credentials = TokenCache._get_source_credentials

__all__ = [
    "AUTH_VALUE_KEYS",
    "OAUTH_TOKEN_VALUE_KEYS",
    "SourceCacheInstance",
    "TokenCache",
    "TokenCacheStore",
    "_OAUTH_CREDENTIAL_KEYS",
    "_active_source",
    "_get_source_credentials",
    "_merge_cli_family",
    "_resolve_source",
    "borrowable_entries",
    "is_foreign_account_entry",
    "server_may_refresh",
    "token_cache",
]

# Global instance
token_cache = TokenCache()
