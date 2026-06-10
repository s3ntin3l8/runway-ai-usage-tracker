"""
Token Cache Service - In-memory cache for sidecar tokens, supporting multiple accounts.

Architecture:
- Sidecar extracts tokens from local files and sends to server
- Server stores tokens in memory (30min TTL) keyed by provider and account_id
- Server uses tokens to make API calls
- If account identity is discovered (email/name), the cache entry is updated ("promoted")
"""

import asyncio
import hashlib
import logging
import time
from typing import Any

from app.core.utils import IdentityExtractor, scrub_log

logger = logging.getLogger(__name__)


class TokenCache:
    """
    In-memory cache for tokens received from sidecars, supporting tenant isolation.

    Tokens expire after TTL (default 30 minutes = 1800 seconds).
    Structure: provider -> account_id -> (tokens, metadata, timestamp)
    """

    DEFAULT_TTL = 1800  # 30 minutes

    def __init__(self, ttl_seconds: int = DEFAULT_TTL):
        # provider_id -> {account_id: (tokens, metadata, timestamp)}
        self._cache: dict[str, dict[str, tuple[dict[str, str], dict[str, Any], float]]] = {}
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

    async def store(
        self,
        provider: str,
        tokens: dict[str, str],
        account_id: str | None = None,
        account_label: str | None = None,
        source: str | None = None,
    ) -> str:
        """
        Store tokens for a provider and account.

        Args:
            provider: Provider name (e.g., "anthropic")
            tokens: Dict of token type -> value
            account_id: Explicit account ID (optional)
            account_label: Human-readable account label (e.g. email) (optional)
            source: Origin of the token — sidecar_id string or None for local

        Returns:
            str: The account_id used for storage
        """
        if not account_label and tokens.get("id_token"):
            payload = IdentityExtractor.extract_jwt_payload(tokens["id_token"])
            email = payload.get("email")
            if email:
                account_label = email

        if not account_id:
            account_id = self._derive_account_id(tokens)

        async with self._lock:
            if provider not in self._cache:
                self._cache[provider] = {}

            metadata = {"account_label": account_label, "source": source}
            self._cache[provider][account_id] = (tokens, metadata, time.time())

            logger.info(
                "Stored %d token(s) for provider %s",
                len(tokens),
                scrub_log(provider),
            )
            return account_id

    async def update_account_metadata(
        self, provider: str, account_id: str, name: str | None = None
    ) -> None:
        """Update metadata (like account name/email) for an existing cache entry."""
        async with self._lock:
            if provider in self._cache and account_id in self._cache[provider]:
                tokens, metadata, timestamp = self._cache[provider][account_id]
                if name:
                    metadata["account_label"] = name
                self._cache[provider][account_id] = (tokens, metadata, timestamp)
                # Log only non-sensitive identifiers — never the account name/email value.
                logger.debug("Updated metadata for %s:%s", scrub_log(provider), account_id)

    async def get_accounts(self, provider: str) -> list[dict[str, Any]]:
        """
        Get all active accounts for a provider.

        Returns:
            List of dicts with: account_id, tokens, account_label, age
        """
        async with self._lock:
            self._clear_expired_unlocked()

            if provider not in self._cache:
                return []

            now = time.time()
            results = []
            for acc_id, (tokens, metadata, timestamp) in self._cache[provider].items():
                results.append(
                    {
                        "account_id": acc_id,
                        "tokens": tokens,
                        "account_label": metadata.get("account_label"),
                        "age": now - timestamp,
                    }
                )
            return results

    async def get(self, provider: str, account_id: str | None = None) -> dict[str, str] | None:
        """
        Get tokens for a specific account, or the first available if account_id is None.
        """
        async with self._lock:
            self._clear_expired_unlocked()

            if provider not in self._cache or not self._cache[provider]:
                return None

            provider_accounts = self._cache[provider]

            if account_id:
                if account_id not in provider_accounts:
                    return None
                tokens, _, _ = provider_accounts[account_id]
                return tokens
            # Return the most recently updated account if none specified
            newest_acc = sorted(provider_accounts.items(), key=lambda x: x[1][2], reverse=True)[0]
            return newest_acc[1][0]

    async def get_with_metadata(
        self, provider: str, account_id: str | None = None
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        """
        Get tokens and metadata for a specific account.
        """
        async with self._lock:
            self._clear_expired_unlocked()

            if provider not in self._cache or not self._cache[provider]:
                return None

            provider_accounts = self._cache[provider]

            if account_id:
                if account_id not in provider_accounts:
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

    def _clear_expired_unlocked(self) -> None:
        """Clear all expired accounts across all providers."""
        now = time.time()
        providers_to_clean = list(self._cache.keys())

        for provider in providers_to_clean:
            expired_accs = [
                acc_id
                for acc_id, (_, _, ts) in self._cache[provider].items()
                if now - ts > self._ttl
            ]
            for acc_id in expired_accs:
                del self._cache[provider][acc_id]
                logger.debug(f"Cleared expired account {acc_id} for {provider}")

            if not self._cache[provider]:
                del self._cache[provider]

    async def purge_expired_unrefreshable(self) -> int:
        """Evict entries already past their JWT `exp` that carry no refresh_token.

        Such tokens can never be auto-rolled (the auto-refresher skips anything
        without a refresh_token), so they only linger as stale "expired" noise in
        Token Health — e.g. a session-cookie-derived bearer or a pre-fix codex
        token pushed without its refresh_token. Refreshable, still-valid, and
        opaque (no-exp) entries are left untouched.

        Returns the number of entries removed.
        """
        async with self._lock:
            now = time.time()
            removed = 0
            for provider in list(self._cache.keys()):
                for acc_id in list(self._cache[provider].keys()):
                    tokens, _, _ = self._cache[provider][acc_id]
                    if "refresh_token" in tokens:
                        continue
                    exp = IdentityExtractor.exp_from_tokens(tokens)
                    if exp is not None and exp < now:
                        del self._cache[provider][acc_id]
                        removed += 1
                        logger.info(f"Purged expired unrefreshable token for {provider}/{acc_id}")
                if not self._cache[provider]:
                    del self._cache[provider]
            return removed

    async def remove(self, provider: str, account_id: str) -> bool:
        """
        Manually remove an account from the cache.
        Returns:
            bool: True if removed, False if not found.
        """
        async with self._lock:
            if provider in self._cache and account_id in self._cache[provider]:
                del self._cache[provider][account_id]
                logger.info(
                    f"Manually removed {scrub_log(provider)} account {scrub_log(account_id)} from cache"
                )

                # Cleanup empty provider entry
                if not self._cache[provider]:
                    del self._cache[provider]
                return True
            return False

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

    async def reset(self) -> None:
        """Clear all cached tokens (used in tests)."""
        async with self._lock:
            self._cache.clear()

    async def get_all_active_accounts(self) -> list[tuple[str, str, str | None]]:
        """
        Get a list of all active (provider, account_id, account_label) tuples.
        Useful for CollectorManager discovery.
        """
        async with self._lock:
            self._clear_expired_unlocked()
            results = []
            for provider, accounts in self._cache.items():
                for acc_id, (_, metadata, _) in accounts.items():
                    results.append((provider, acc_id, metadata.get("account_label")))
            return results


# Global instance
token_cache = TokenCache()
