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
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.core.utils import IdentityExtractor, scrub_log
from app.services.account_identity import canonical_account_id

logger = logging.getLogger(__name__)

_active_source: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "active_credential_source", default=None
)
_active_attempt: ContextVar[dict[str, bool] | None] = ContextVar(
    "active_credential_attempt", default=None
)

_OAUTH_CREDENTIAL_KEYS = {
    "oauth_token",
    "refresh_token",
    "id_token",
    "expiry_date",
    "client_id",
}
AUTH_VALUE_KEYS = frozenset(
    {
        "api_key",
        "oauth_token",
        "access_token",
        "refresh_token",
        "id_token",
        "xai_access",
        "xai_refresh",
        "cli_access_token",
        "session_cookie",
    }
)
OAUTH_TOKEN_VALUE_KEYS = AUTH_VALUE_KEYS - {"api_key", "session_cookie"}

# Origins the user typed into the dashboard. They outrank every other origin
# (sidecar ids, "server") when a later push re-stamps `source`: a sidecar
# pushing *any* credential for the same account must not reclassify an
# explicit paste, or collectors read `input_source=sidecar` for the pasted
# key and a 401 on it stops being treated as authoritative (PR #352 review).
_EXPLICIT_SOURCES = frozenset({"config", "manual_config"})


def _resolve_source(incoming: str | None, existing: str | None) -> str | None:
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
        # Track each credential family's last report independently. Sidecars
        # send one card per origin, so refreshing a CLI token must not keep a
        # removed browser cookie alive forever (or vice versa).
        self._token_timestamps: dict[str, dict[str, dict[str, float]]] = {}
        # Credential bundles stay separate here even though the legacy account
        # cache below remains merged for backwards-compatible consumers.
        self._source_cache: dict[
            str, dict[str, dict[str, tuple[dict[str, str], dict[str, Any], float]]]
        ] = {}
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
        source_id: str | None = None,
        source_metadata: dict[str, Any] | None = None,
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
        selection = _active_source.get()
        if source_id is None and selection and selection[0] == provider:
            source_id = selection[2]

        if not account_label and tokens.get("id_token"):
            payload = IdentityExtractor.extract_jwt_payload(tokens["id_token"])
            email = payload.get("email")
            if email:
                account_label = email

        if not account_id:
            account_id = (
                selection[1]
                if selection and selection[0] == provider
                else self._derive_account_id(tokens)
            )
        # Key by the canonical form so a sidecar-pushed ``Alice@X.com`` and
        # the collector's ``alice@x.com`` share one cache slot.
        account_id = canonical_account_id(account_id)

        # Store each source as a self-contained credential bundle. The merged
        # cache below is preserved until all existing callers migrate to the
        # source-aware read path.
        async with self._lock:
            if source_id:
                source_accounts = self._source_cache.setdefault(provider, {}).setdefault(
                    account_id, {}
                )
                previous = source_accounts.get(source_id)
                stored_tokens = dict(previous[0]) if previous else {}
                if previous and self._is_staler(tokens, stored_tokens):
                    for key, value in tokens.items():
                        if key not in _OAUTH_CREDENTIAL_KEYS and key not in stored_tokens:
                            stored_tokens[key] = value
                    if tokens.get("refresh_token"):
                        stored_tokens["refresh_token"] = tokens["refresh_token"]
                else:
                    stored_tokens.update(tokens)
                metadata = dict(previous[1]) if previous else {}
                metadata.update(source_metadata or {})
                metadata.update({"source_id": source_id, "source": source})
                source_accounts[source_id] = (stored_tokens, metadata, time.time())

            # Keep identity-pending credentials available only through the
            # source-pinned API path. They must not become a visible/default
            # account in the compatibility cache before verification.
            if (source_metadata or {}).get("identity_pending") is True:
                return account_id

            if provider not in self._cache:
                self._cache[provider] = {}
                self._token_timestamps[provider] = {}

            existing = self._cache[provider].get(account_id)
            if existing is not None and self._is_staler(tokens, existing[0]):
                # A staler credential must not downgrade a fresher cached one — a
                # sidecar re-pushing its local (expired) access token would
                # otherwise clobber the server-refreshed token every cycle. Keep
                # the fresher tokens, but absorb a rotated refresh_token and fill
                # in identity metadata. Only fields actually reported again
                # have their independent TTL refreshed.
                kept_tokens, kept_meta, _ = existing
                # Keep the fresher OAuth family, while retaining independent
                # credential families (for example a browser cookie beside a
                # CLI OAuth token) pushed for this same account.
                for key, value in tokens.items():
                    self._mark_token_seen(provider, account_id, key)
                    if key not in _OAUTH_CREDENTIAL_KEYS:
                        if key not in kept_tokens:
                            kept_tokens[key] = value
                if tokens.get("refresh_token"):
                    kept_tokens["refresh_token"] = tokens["refresh_token"]
                if account_label and not kept_meta.get("account_label"):
                    kept_meta["account_label"] = account_label
                if source:
                    kept_meta["source"] = _resolve_source(source, kept_meta.get("source"))
                self._cache[provider][account_id] = (kept_tokens, kept_meta, time.time())
                logger.info(
                    "Kept fresher cached token for provider %s (ignored staler push)",
                    scrub_log(provider),
                )
                return account_id

            # Preserve prior identity metadata when an incoming store omits it — a
            # server-side refresh that doesn't carry `source` must not erase the
            # sidecar origin recorded by a previous push. A truthy incoming value
            # still wins (fresher push from another sidecar, a `source="config"`
            # store, etc.), except that an explicit dashboard origin is never
            # downgraded by a sidecar/server push (`_resolve_source`).
            prev_meta = existing[1] if existing is not None else {}
            stored_tokens = {**existing[0], **tokens} if existing is not None else tokens
            for key in tokens:
                self._mark_token_seen(provider, account_id, key)
            metadata = {
                "account_label": account_label or prev_meta.get("account_label"),
                "source": _resolve_source(source, prev_meta.get("source")),
                "identity_pending": bool(
                    (source_metadata or {}).get(
                        "identity_pending", prev_meta.get("identity_pending", False)
                    )
                ),
            }
            self._cache[provider][account_id] = (stored_tokens, metadata, time.time())

            logger.info(
                "Stored %d token(s) for provider %s",
                len(stored_tokens),
                scrub_log(provider),
            )
            return account_id

    def _mark_token_seen(self, provider: str, account_id: str, key: str) -> None:
        """Record when a particular credential field was last reported."""
        self._token_timestamps.setdefault(provider, {}).setdefault(account_id, {})[key] = (
            time.time()
        )

    @staticmethod
    def _is_staler(incoming: dict[str, str], existing: dict[str, str]) -> bool:
        """True when *incoming* should not be allowed to replace *existing*.

        A token with a *known* expiry already in the past is treated as
        maximally stale regardless of the existing entry's expiry — this
        guards against a sidecar with no comparable expiry signal on the
        existing side (e.g. an unpatched binary, or a provider whose existing
        entry predates an expiry-stamping fix) still holding a valid token
        while a different sidecar re-pushes its own expired credential. Without
        this, `exist_exp is None` would make the two sides incomparable and the
        expired push would win on recency alone (this is exactly how one
        sidecar's expired Antigravity token clobbered another's valid one).

        Otherwise, only meaningful when BOTH carry a comparable expiry
        (`exp_from_tokens` — JWT `exp` or `expiry_date`). Opaque credentials
        (api keys, cookies) yield None on either side, so this returns False
        and the normal overwrite wins.
        """
        inc_exp = IdentityExtractor.exp_from_tokens(incoming)
        if inc_exp is not None and inc_exp < time.time():
            return True
        exist_exp = IdentityExtractor.exp_from_tokens(existing)
        if inc_exp is None or exist_exp is None:
            return False
        return inc_exp < exist_exp

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

            if provider not in self._cache or not self._cache[provider]:
                return None

            provider_accounts = self._cache[provider]

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
        self, provider: str, account_id: str | None = None
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        """
        Get tokens and metadata for a specific account.
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

            if provider not in self._cache or not self._cache[provider]:
                return None

            provider_accounts = self._cache[provider]

            if account_id:
                if account_id not in provider_accounts:
                    # Same "default" fallback as get() — config creds live under
                    # "default" regardless of the collector's resolved identity.
                    if "default" in provider_accounts:
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
        """Mark the active bundle rejected when its credential gets HTTP 401."""
        attempt = _active_attempt.get()
        if attempt is None or getattr(response, "status_code", None) != 401:
            return

        request = getattr(response, "request", None)
        if request is not None and not self._request_uses_active_source(request):
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

    async def remove_source(self, provider: str, account_id: str, source_id: str) -> bool:
        """Remove one live secret bundle while preserving its durable metadata."""
        account_id = canonical_account_id(account_id)
        async with self._lock:
            sources = self._source_cache.get(provider, {}).get(account_id)
            if not sources or source_id not in sources:
                return False
            del sources[source_id]
            if not sources:
                self._source_cache[provider].pop(account_id, None)
            if not self._source_cache.get(provider):
                self._source_cache.pop(provider, None)
            return True

    async def move_source(
        self, provider: str, from_account_id: str, to_account_id: str, source_id: str
    ) -> bool:
        """Move an identified credential bundle and clear ``identity_pending``.

        Call only after the source has been tagged or otherwise verified; the
        destination entry is always marked as no longer pending.
        """
        from_id = canonical_account_id(from_account_id)
        to_id = canonical_account_id(to_account_id)
        async with self._lock:
            source_accounts = self._source_cache.get(provider, {}).get(from_id, {})
            entry = source_accounts.pop(source_id, None)
            if entry is None:
                return False
            if not source_accounts:
                self._source_cache[provider].pop(from_id, None)
            tokens, metadata, timestamp = entry
            entry = (tokens, {**metadata, "identity_pending": False}, timestamp)
            self._source_cache.setdefault(provider, {}).setdefault(to_id, {})[source_id] = entry
            # Keep the compatibility cache coherent when it represents this
            # same source bundle; never overwrite another account's aggregate.
            old_aggregate = self._cache.get(provider, {}).get(from_id)
            if old_aggregate and old_aggregate[1].get("source_id") == source_id:
                self._cache.setdefault(provider, {})[to_id] = old_aggregate
                self._cache[provider].pop(from_id, None)
            elif entry:
                tokens, metadata, timestamp = entry
                target_aggregate = self._cache.setdefault(provider, {}).get(to_id)
                if target_aggregate is None:
                    self._cache[provider][to_id] = (
                        dict(tokens),
                        {
                            "account_label": metadata.get("account_label"),
                            "source": metadata.get("source"),
                            "source_id": source_id,
                        },
                        timestamp,
                    )
                else:
                    target_aggregate[0].update(tokens)
                    for key in tokens:
                        self._mark_token_seen(provider, to_id, key)
            return True

    async def remove_source_tokens(
        self, provider: str, account_id: str, source_id: str, token_types: set[str]
    ) -> None:
        account_id = canonical_account_id(account_id)
        async with self._lock:
            sources = self._source_cache.get(provider, {}).get(account_id, {})
            entry = sources.get(source_id)
            if not entry:
                return
            tokens, metadata, timestamp = entry
            for token_type in token_types:
                tokens.pop(token_type, None)
            if tokens:
                sources[source_id] = (tokens, metadata, timestamp)
            else:
                sources.pop(source_id, None)

    def _clear_expired_unlocked(self) -> None:
        """Clear all expired accounts across all providers."""
        now = time.time()
        for provider in list(self._source_cache):
            for account_id in list(self._source_cache[provider]):
                sources = self._source_cache[provider][account_id]
                for source_id, (_tokens, _metadata, timestamp) in list(sources.items()):
                    if now - timestamp > self._ttl:
                        del sources[source_id]
                if not sources:
                    del self._source_cache[provider][account_id]
            if not self._source_cache[provider]:
                del self._source_cache[provider]
        providers_to_clean = list(self._cache.keys())

        for provider in providers_to_clean:
            expired_accs = []
            for acc_id, (tokens, metadata, ts) in list(self._cache[provider].items()):
                key_timestamps = self._token_timestamps.setdefault(provider, {}).setdefault(
                    acc_id, {}
                )
                # Entries seeded by older code/tests have no per-key timestamps;
                # inherit the account timestamp to preserve their existing TTL.
                for key in tokens:
                    key_timestamps.setdefault(key, ts)
                for key in list(tokens):
                    if now - key_timestamps.get(key, ts) > self._ttl:
                        tokens.pop(key, None)
                        key_timestamps.pop(key, None)
                if not tokens:
                    expired_accs.append(acc_id)
                else:
                    self._cache[provider][acc_id] = (
                        tokens,
                        metadata,
                        max(key_timestamps.values(), default=ts),
                    )
            for acc_id in expired_accs:
                del self._cache[provider][acc_id]
                self._token_timestamps.get(provider, {}).pop(acc_id, None)
                logger.debug(f"Cleared expired account {acc_id} for {provider}")

            if not self._cache[provider]:
                del self._cache[provider]
                self._token_timestamps.pop(provider, None)

    async def purge_expired_unrefreshable(self) -> int:
        """Strip the dead OAuth family from entries past their JWT `exp` with no refresh_token.

        Such tokens can never be auto-rolled (the auto-refresher skips anything
        without a refresh_token), so they only linger as stale credentials —
        e.g. a session-cookie-derived bearer or a pre-fix codex token pushed
        without its refresh_token. Only the expired OAuth fields are removed:
        an independent credential family stored beside them (a browser cookie,
        an API key) stays. An entry whose *only* credentials are the expired
        token is retained, so Token Health still reports the account as dead
        instead of silently forgetting it (it ages out via the TTL if the
        sidecar stops pushing it).

        Returns the number of entries whose expired OAuth fields were removed.
        """
        async with self._lock:
            now = time.time()
            removed = 0
            for provider in list(self._cache.keys()):
                for acc_id in list(self._cache[provider].keys()):
                    tokens, metadata, ts = self._cache[provider][acc_id]
                    if "refresh_token" in tokens:
                        continue
                    exp = IdentityExtractor.exp_from_tokens(tokens)
                    if exp is None or exp >= now:
                        continue
                    dead = (_OAUTH_CREDENTIAL_KEYS | {"access_token"}) & tokens.keys()
                    if not dead or dead == tokens.keys():
                        # Nothing else to keep: retain the sole expired entry
                        # as evidence for Token Health.
                        continue
                    timestamps = self._token_timestamps.setdefault(provider, {}).setdefault(
                        acc_id, {}
                    )
                    for key in dead:
                        tokens.pop(key, None)
                        timestamps.pop(key, None)
                    self._cache[provider][acc_id] = (
                        tokens,
                        metadata,
                        max(timestamps.values(), default=ts),
                    )
                    removed += 1
                    logger.info(
                        "Purged expired unrefreshable OAuth fields for %s/%s",
                        scrub_log(provider),
                        scrub_log(acc_id),
                    )
            return removed

    def seed_sync(
        self,
        provider: str,
        account_id: str,
        tokens: dict[str, str],
        metadata: dict[str, Any] | None = None,
        last_seen: float | None = None,
    ) -> None:
        """Synchronously seed an entry without acquiring the asyncio lock.

        Intended for tests: awaiting ``store()`` from a throwaway event loop
        would bind ``_lock`` to that loop so later TestClient requests cannot
        acquire it. Writes the same ``(tokens, metadata, last_seen)`` layout
        that ``store()`` uses.
        """
        if provider not in self._cache:
            self._cache[provider] = {}
        self._token_timestamps.setdefault(provider, {})[account_id] = {
            key: (last_seen or time.time()) for key in tokens
        }
        self._cache[provider][account_id] = (tokens, metadata or {}, last_seen or time.time())

    async def remove(self, provider: str, account_id: str) -> bool:
        """
        Manually remove an account from the cache.
        Returns:
            bool: True if removed, False if not found.
        """
        account_id = canonical_account_id(account_id)
        async with self._lock:
            if provider in self._cache and account_id in self._cache[provider]:
                del self._cache[provider][account_id]
                self._token_timestamps.get(provider, {}).pop(account_id, None)
                self._source_cache.get(provider, {}).pop(account_id, None)
                logger.info(
                    f"Manually removed {scrub_log(provider)} account {scrub_log(account_id)} from cache"
                )

                # Cleanup empty provider entry
                if not self._cache[provider]:
                    del self._cache[provider]
                    self._token_timestamps.pop(provider, None)
                return True
            return False

    async def remove_tokens(self, provider: str, account_id: str, token_types: set[str]) -> None:
        """Remove selected fields while preserving other credential families."""
        account_id = canonical_account_id(account_id)
        async with self._lock:
            provider_entries = self._cache.get(provider)
            if not provider_entries or account_id not in provider_entries:
                return
            tokens, metadata, timestamp = provider_entries[account_id]
            timestamps = self._token_timestamps.setdefault(provider, {}).setdefault(account_id, {})
            for token_type in token_types:
                tokens.pop(token_type, None)
                timestamps.pop(token_type, None)
            if not tokens:
                del provider_entries[account_id]
                self._token_timestamps.get(provider, {}).pop(account_id, None)
                if not provider_entries:
                    self._cache.pop(provider, None)
                    self._token_timestamps.pop(provider, None)
                return
            provider_entries[account_id] = (
                tokens,
                metadata,
                max(timestamps.values(), default=timestamp),
            )

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
            self._token_timestamps.clear()
            self._source_cache.clear()

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
