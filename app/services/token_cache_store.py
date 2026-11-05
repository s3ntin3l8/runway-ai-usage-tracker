"""Persistence and expiry for the in-memory token cache.

``TokenCache`` mixes this in. The methods are moved verbatim, so they close
over this module's globals rather than ``token_cache``'s.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from app.core.utils import IdentityExtractor, has_refresh_credential, scrub_log
from app.services.account_identity import canonical_account_id
from app.services.probe_mode import is_probing
from app.services.token_cache_sources import (
    _CLI_FAMILY_KEYS,
    _OAUTH_CREDENTIAL_KEYS,
    _active_source,
    _credential_expiry,
    _merge_cli_family,
    _resolve_source,
)

logger = logging.getLogger(__name__)


class TokenCacheStore:
    """Store, remove, expire, and relocate credential bundles.

    Instance state is created in ``TokenCache.__init__``. The annotations
    exist so type-checking the mixin sees the attributes it already uses.
    """

    _cache: dict[str, Any]
    _token_timestamps: dict[str, Any]
    _source_cache: dict[str, Any]
    _durable_revision: int
    _durable_source_ids: set[str]
    _durable_bundles: dict[str, Any]
    _ttl: int
    _lock: Any

    def __getattr__(self, name: str) -> Any:
        """Read-side methods stay on ``TokenCache`` and are found there first."""
        raise AttributeError(name)

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
        if is_probing():
            # A probe only looks: whatever a collector would remember (a mirrored login file, an
            # exchanged bearer, a refreshed token) is not stored.
            return canonical_account_id(account_id or "default")
        if "cli_device_id" in tokens and not tokens.get("cli_access_token"):
            tokens = {key: value for key, value in tokens.items() if key != "cli_device_id"}
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
            self._clear_expired_unlocked()
            if source_id:
                previous = self._source_cache.get(provider, {}).get(account_id, {}).get(source_id)
                if previous and previous[1].get("durable_source") is True:
                    from app.services.source_controls import replace_cached_source

                    replacement = {**previous[0], **tokens}
                    if not self._is_staler(tokens, previous[0]):
                        replace_cached_source(
                            provider, source_id, account_id, previous[0], replacement
                        )
                    self._durable_revision = -1
                    self._reload_durable_unlocked()
                    return account_id
                if source_id in self._durable_source_ids:
                    return account_id
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
                    if tokens.get("xai_refresh") and "xai_refresh" not in stored_tokens:
                        stored_tokens["xai_refresh"] = tokens["xai_refresh"]
                else:
                    _merge_cli_family(stored_tokens, tokens, prefer_incoming=True)
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
                if tokens.get("xai_refresh") and "xai_refresh" not in kept_tokens:
                    kept_tokens["xai_refresh"] = tokens["xai_refresh"]
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
            stored_tokens = dict(existing[0]) if existing is not None else {}
            _merge_cli_family(stored_tokens, tokens, prefer_incoming=True)
            stored_tokens.update(tokens)
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

    def _reload_durable_unlocked(self) -> None:
        from app.services.source_controls import read_snapshot

        snapshot = read_snapshot(self._durable_revision, self._durable_bundles)
        if snapshot is None:
            return
        revision, rows = snapshot
        for accounts in self._source_cache.values():
            for sources in accounts.values():
                for source_id, entry in list(sources.items()):
                    if entry[1].get("durable_source") is True:
                        del sources[source_id]
        for row in rows:
            self._durable_source_ids.add(row["source_id"])
            sources = self._source_cache.setdefault(row["provider_id"], {}).setdefault(
                canonical_account_id(row["account_id"]), {}
            )
            sources[row["source_id"]] = (dict(row["tokens"]), dict(row["metadata"]), time.time())
        self._durable_revision = revision

    def _mark_token_seen(self, provider: str, account_id: str, key: str) -> None:
        """Record when a particular credential field was last reported."""
        self._token_timestamps.setdefault(provider, {}).setdefault(account_id, {})[key] = (
            time.time()
        )

    @staticmethod
    def _is_staler(incoming: dict[str, str], existing: dict[str, str]) -> bool:
        """True when *incoming* should not be allowed to replace *existing*.

        A spent incoming access token is maximally stale only while the cached
        side still has life — or carries no expiry to compare against (e.g. an
        unpatched binary, or a provider whose existing entry predates an
        expiry-stamping fix). Without that second clause `exist_exp is None`
        would make the two sides incomparable and the expired push would win
        on recency alone (this is exactly how one sidecar's expired
        Antigravity token clobbered another's valid one).

        When BOTH sides are already spent, the newer lineage wins instead of
        the incoming one being rejected out of hand. That is what lets a
        machine that has just rotated its refresh token deliver it on its next
        push: between CLI rotations every access JWT it reports is expired, so
        an unconditional "expired means maximally stale" rule would pin a dead
        refresh token in the cache forever (issue #474).

        Otherwise only meaningful when BOTH carry a comparable expiry
        (`exp_from_tokens` — JWT `exp` or `expiry_date`). Opaque credentials
        (api keys, cookies) yield None on either side, so this returns False
        and the normal overwrite wins.
        """
        now = time.time()
        inc_exp = _credential_expiry(incoming)
        exist_exp = _credential_expiry(existing)
        if inc_exp is not None and inc_exp < now:
            if exist_exp is None or exist_exp >= now:
                return True
            # Both lineages are spent — fall through and compare them.
        if inc_exp is None or exist_exp is None:
            return False
        return inc_exp < exist_exp

    async def remove_source(
        self,
        provider: str,
        account_id: str,
        source_id: str,
        *,
        retire_matching_oauth: bool = False,
    ) -> bool:
        """Remove one live secret bundle while preserving its durable metadata.

        ``retire_matching_oauth`` also strips the account aggregate's OAuth fields that came
        from this source — but never one a *remaining* source of the account still holds: a
        dead copy of a login that is also the active one must not take the live token with it.
        """
        account_id = canonical_account_id(account_id)
        async with self._lock:
            sources = self._source_cache.get(provider, {}).get(account_id)
            if not sources or source_id not in sources:
                return False
            source_tokens = sources[source_id][0]
            others = [entry[0] for sid, entry in sources.items() if sid != source_id]
            # Durable bundles never enter the legacy aggregate. An identical
            # token there belongs to a separate manual/file contribution.
            if retire_matching_oauth and sources[source_id][1].get("durable_source") is not True:
                aggregate = self._cache.get(provider, {}).get(account_id)
                if aggregate:
                    tokens, metadata, timestamp = aggregate
                    key_timestamps = self._token_timestamps.get(provider, {}).get(account_id, {})
                    # A pending Claude source uses its source_id as the account
                    # key, so that aggregate belongs exclusively to this source.
                    # For shared account aggregates, remove only OAuth fields
                    # whose values still match the source being retired.
                    retire_all_oauth = account_id == source_id
                    oauth_keys = _OAUTH_CREDENTIAL_KEYS | {"access_token"}
                    keys_to_remove = {
                        key
                        for key in oauth_keys
                        if retire_all_oauth
                        or (
                            source_tokens.get(key)
                            and source_tokens[key] == tokens.get(key)
                            and not any(o.get(key) == source_tokens[key] for o in others)
                        )
                    }
                    if "cli_access_token" in keys_to_remove:
                        keys_to_remove.update(_CLI_FAMILY_KEYS)
                    # `tokens` aliases the live dict in `aggregate`; pop in
                    # place before reassigning the tuple.
                    for key in keys_to_remove:
                        tokens.pop(key, None)
                        key_timestamps.pop(key, None)
                    if keys_to_remove:
                        metadata = {**metadata, "identity_pending": False}
                        if tokens:
                            self._cache[provider][account_id] = (tokens, metadata, timestamp)
                        else:
                            del self._cache[provider][account_id]
                            self._token_timestamps.get(provider, {}).pop(account_id, None)
                            if not self._cache[provider]:
                                del self._cache[provider]
                                self._token_timestamps.pop(provider, None)
            del sources[source_id]
            if not sources:
                self._source_cache[provider].pop(account_id, None)
            if not self._source_cache.get(provider):
                self._source_cache.pop(provider, None)
            return True

    async def commit_verified_source(
        self,
        provider: str,
        from_account_id: str,
        to_account_id: str,
        source_id: str,
        expected_generation: str,
        commit: Callable[[], bool],
    ) -> bool:
        """Compare the proved bundle, commit its durable binding, and move it without yielding.

        The synchronous callback owns the DB transaction. Holding the existing cache lock
        prevents ingest/removal/reconciliation from replacing the bundle around that commit.
        """
        from_id = canonical_account_id(from_account_id)
        to_id = canonical_account_id(to_account_id)
        async with self._lock:
            self._clear_expired_unlocked()
            entry = self._source_cache.get(provider, {}).get(from_id, {}).get(source_id)
            if entry is None or self.credential_generation(entry[0]) != expected_generation:
                return False
            if not commit():
                return False
            return self._move_source_unlocked(provider, from_id, to_id, source_id)

    async def move_source(
        self,
        provider: str,
        from_account_id: str,
        to_account_id: str,
        source_id: str,
        *,
        expected_generation: str | None = None,
    ) -> bool:
        """Move an identified credential bundle and clear ``identity_pending``.

        Call only after the source has been tagged or otherwise verified; the
        destination entry is always marked as no longer pending.
        """
        from_id = canonical_account_id(from_account_id)
        to_id = canonical_account_id(to_account_id)
        async with self._lock:
            entry = self._source_cache.get(provider, {}).get(from_id, {}).get(source_id)
            if expected_generation is not None and (
                entry is None or self.credential_generation(entry[0]) != expected_generation
            ):
                return False
            return self._move_source_unlocked(provider, from_id, to_id, source_id)

    def _move_source_unlocked(
        self, provider: str, from_id: str, to_id: str, source_id: str
    ) -> bool:
        source_accounts = self._source_cache.get(provider, {}).get(from_id, {})
        current = source_accounts.get(source_id)
        if current and current[1].get("durable_source") is True:
            from app.services.source_controls import promote_cached_source

            promoted = promote_cached_source(provider, source_id, from_id, to_id, current[0])
            self._durable_revision = -1
            self._reload_durable_unlocked()
            return promoted
        entry = source_accounts.pop(source_id, None)
        if entry is None:
            return False
        if not source_accounts:
            self._source_cache[provider].pop(from_id, None)
        tokens, metadata, timestamp = entry
        target_sources = self._source_cache.setdefault(provider, {}).setdefault(to_id, {})
        existing_target = target_sources.get(source_id)
        # Only an already-live entry for this exact source can win the
        # per-key expiry comparison against the bundle being promoted.
        if existing_target:
            target_tokens, target_metadata, target_timestamp = existing_target
            merged_tokens = dict(target_tokens)
            from_oauth_is_staler = self._is_staler(tokens, target_tokens)
            _merge_cli_family(merged_tokens, tokens, prefer_incoming=not from_oauth_is_staler)
            for key, value in tokens.items():
                if key in _CLI_FAMILY_KEYS:
                    continue
                if key in merged_tokens:
                    if key in _OAUTH_CREDENTIAL_KEYS and self._is_staler(
                        {key: merged_tokens[key]}, {key: value}
                    ):
                        merged_tokens[key] = value
                    continue
                if from_oauth_is_staler and key in _OAUTH_CREDENTIAL_KEYS:
                    continue
                merged_tokens[key] = value
            # Existing target metadata wins on collisions, preserving its
            # source ownership and last-seen details during promotion.
            merged_metadata = {**metadata, **target_metadata, "identity_pending": False}
            entry = (merged_tokens, merged_metadata, max(timestamp, target_timestamp))
        else:
            entry = (tokens, {**metadata, "identity_pending": False}, timestamp)
        target_sources[source_id] = entry
        # Keep the compatibility cache coherent when it represents this
        # same source bundle; never overwrite another account's aggregate.
        old_aggregate = self._cache.get(provider, {}).get(from_id)
        if from_id != to_id and old_aggregate and old_aggregate[1].get("source_id") == source_id:
            target_aggregate = self._cache.setdefault(provider, {}).get(to_id)
            if target_aggregate is None:
                self._cache[provider][to_id] = old_aggregate
            else:
                _merge_cli_family(
                    target_aggregate[0],
                    old_aggregate[0],
                    prefer_incoming=not self._is_staler(old_aggregate[0], target_aggregate[0]),
                )
                for key, value in old_aggregate[0].items():
                    if key in _CLI_FAMILY_KEYS:
                        continue
                    if key in target_aggregate[0]:
                        # Keep the better known-expiry OAuth value when a
                        # stale target survived an earlier legacy migration.
                        if key in _OAUTH_CREDENTIAL_KEYS and self._is_staler(
                            {key: target_aggregate[0][key]}, {key: value}
                        ):
                            target_aggregate[0][key] = value
                            self._mark_token_seen(provider, to_id, key)
                        continue
                    if self._is_staler({key: value}, target_aggregate[0]):
                        # Use the target's full expiry context, not just the
                        # missing key, so another OAuth field can veto it.
                        # Opaque non-OAuth keys have no expiry to compare and pass.
                        continue
                    target_aggregate[0][key] = value
                    self._mark_token_seen(provider, to_id, key)
                if not target_aggregate[1].get("account_label"):
                    target_aggregate[1]["account_label"] = old_aggregate[1].get("account_label")
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
                prefer_cli = not self._is_staler(tokens, target_aggregate[0])
                _merge_cli_family(target_aggregate[0], tokens, prefer_incoming=prefer_cli)
                target_aggregate[0].update(
                    {key: value for key, value in tokens.items() if key not in _CLI_FAMILY_KEYS}
                )
                for key in tokens:
                    if key not in _CLI_FAMILY_KEYS or prefer_cli:
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
            if "cli_access_token" in token_types:
                token_types = token_types | _CLI_FAMILY_KEYS
            for token_type in token_types:
                tokens.pop(token_type, None)
            if tokens:
                sources[source_id] = (tokens, metadata, timestamp)
            else:
                sources.pop(source_id, None)

    def _clear_expired_unlocked(self) -> None:
        """Reload durable contributions and expire only legacy heartbeat entries."""
        self._reload_durable_unlocked()
        now = time.time()
        for provider in list(self._source_cache):
            for account_id in list(self._source_cache[provider]):
                sources = self._source_cache[provider][account_id]
                for source_id, (_tokens, _metadata, timestamp) in list(sources.items()):
                    if _metadata.get("durable_source") is not True and now - timestamp > self._ttl:
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
                    if has_refresh_credential(tokens):
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

        Removes the merged account entry *and* every source bundle of that
        account, whether or not the merged entry exists: a durable bundle
        (an enrolled machine's credential) is written to the source cache
        only — ``store()`` skips the merged entry for a durable source — so
        dropping the merged entry alone would leave the credential behind.

        Returns:
            bool: True if anything was removed, False if the account held
            nothing.
        """
        account_id = canonical_account_id(account_id)
        async with self._lock:
            removed = False
            if provider in self._cache and account_id in self._cache[provider]:
                del self._cache[provider][account_id]
                removed = True
            self._token_timestamps.get(provider, {}).pop(account_id, None)
            if self._source_cache.get(provider, {}).pop(account_id, None) is not None:
                removed = True
            if not removed:
                return False
            logger.info(
                f"Manually removed {scrub_log(provider)} account {scrub_log(account_id)} from cache"
            )

            # Cleanup empty provider entries
            if not self._cache.get(provider):
                self._cache.pop(provider, None)
                self._token_timestamps.pop(provider, None)
            if not self._source_cache.get(provider):
                self._source_cache.pop(provider, None)
            return True

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

    async def reset(self) -> None:
        """Clear all cached tokens (used in tests)."""
        async with self._lock:
            self._cache.clear()
            self._token_timestamps.clear()
            self._source_cache.clear()
            self._durable_revision = -1
            self._durable_source_ids.clear()
            self._durable_bundles.clear()
