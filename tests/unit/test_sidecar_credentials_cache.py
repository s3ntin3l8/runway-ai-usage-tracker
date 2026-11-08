"""Unit tests for CredentialCache state, freshness, and replacement semantics.

Covers:
- Identity and token decoupled views (PR #272, #283).
- Freshness tracking and TTL expiry.
- Outage tolerance and preserving prior snapshot during failed refresh.
- Single HTTP round-trip on refresh_from_config when fetching tokens.
- Tag hints cache storage, defensive copies, and fallback retention.
"""

from __future__ import annotations

from unittest.mock import patch

from tests.unit._sidecar_credentials_helpers import (  # noqa: F401
    approved_keychain_readers,
    isolate_host_credentials,
)


class TestCredentialCache:
    def test_provider_accounts_includes_rows_without_tokens(self):
        """The cache's provider_accounts() returns the identity view —
        every server-known row regardless of whether a token was issued.
        This is the core of the #272 fix (PR #283 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        # Identity map populated; token map deliberately empty (simulates
        # INGEST_API_KEY-empty config where no tokens are issued).
        cache.replace(
            accounts={
                "anthropic": ["default", "alice@example.com"],
                "chatgpt": ["default"],
            },
        )
        # provider_accounts still works.
        result = cache.provider_accounts()
        assert result == {
            "anthropic": ["default", "alice@example.com"],
            "chatgpt": ["default"],
        }
        # And the token map is empty — proves identity was decoupled.
        assert cache.tokens == {}

    def test_replace_replaces_prior_state(self):
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(accounts={"anthropic": ["default", "alice@example.com"]})
        assert cache.provider_accounts() == {"anthropic": ["default", "alice@example.com"]}
        # Re-replacing drops the prior set.
        cache.replace(accounts={"anthropic": ["bob@example.com"]})
        assert cache.provider_accounts() == {"anthropic": ["bob@example.com"]}

    def test_provider_accounts_order_is_arbitrary(self):
        """Explicit guardrail: the order mirrors JSON serialization and
        must not be relied on for positional lookup. Callers that pick
        ``provider_accounts[provider_id][0]`` will fail (PR #283 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(accounts={"anthropic": ["alice@example.com", "default"]})
        # The order is whatever the server returned; we don't reorder.
        accounts = cache.provider_accounts()["anthropic"]
        assert accounts == ["alice@example.com", "default"]
        # And consumers (run_collection) must use ``in`` checks, not
        # ``[0]`` indexing. This test simply pins the current behavior
        # so a future "sort for determinism" change is intentional.

    def test_is_fresh(self):
        import time as _t

        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache(ttl_seconds=60)
        # No fetch yet → not fresh
        assert not cache.is_fresh()
        cache.replace(accounts={})
        baseline = _t.time()
        # Fresh immediately after fetch (delta 0 < 60)
        assert cache.is_fresh(now=baseline)
        # Stale after TTL
        assert not cache.is_fresh(now=baseline + 61)

    def test_replace_preserves_view_when_argument_is_none(self):
        """``replace(accounts=None, tokens=X)`` keeps the existing
        identity view, and vice versa. Lets ``run_collection`` skip
        either side independently when its fetch returned None (PR #283
        round-3 review). ``tag_hints=None`` preserves the prior hint map
        — used by ``run_collection`` when the server omits the
        ``account_tag_hints`` field (older server pre-PR #288), so a
        downgrade doesn't silently wipe cached hints (PR #290
        round-2 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            accounts={"anthropic": ["default"]},
            tokens={("anthropic", "default"): "tok"},
            tag_hints={
                "anthropic": {"provider:anthropic": "default"},
            },
        )
        # Replace only tokens → identity + hint views intact.
        cache.replace(tokens={("anthropic", "default"): "tok2"})
        assert cache.provider_accounts() == {"anthropic": ["default"]}
        assert cache.tokens == {("anthropic", "default"): "tok2"}
        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "default"},
        }
        # And ``tag_hints=None`` keeps the prior hint map.
        cache.replace(tag_hints=None)
        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "default"},
        }, "tag_hints=None must preserve the prior hint map"
        # But ``tag_hints={}`` clears it.
        cache.replace(tag_hints={})
        assert cache.provider_tag_hints() == {}, "tag_hints={} must clear the hint map"

    def test_refresh_from_config_keeps_prior_snapshot_on_outage(self):
        """When the server is unreachable, ``refresh_from_config`` must
        return ``None`` AND leave the cache untouched — so the next cycle
        retries instead of silently working off an empty map for the
        entire TTL (PR #283 round-3 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            accounts={"anthropic": ["default"]},
            tokens={("anthropic", "default"): "tok"},
        )

        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            return_value=None,
        ):
            result = cache.refresh_from_config("https://api.example.com", fetch_tokens=True)

        assert result is None
        # Cache state untouched.
        assert cache.provider_accounts() == {"anthropic": ["default"]}
        assert cache.tokens == {("anthropic", "default"): "tok"}

    def test_refresh_from_config_single_round_trip_when_fetch_tokens_true(self):
        """``refresh_from_config(fetch_tokens=True)`` must share ONE
        underlying HTTP fetch — both views deserialize from the same
        payload (PR #283 round-3 review). Two consecutive
        ``_fetch_config_payload`` calls (one per view) would double the
        request load on every heartbeat."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        payload = {
            "config": {
                "providers": {
                    "anthropic": {
                        "accounts": [
                            {
                                "account_id": "default",
                                "credential_token": "tok",
                                "enabled": True,
                            }
                        ]
                    }
                }
            }
        }

        call_count = {"n": 0}

        def _counted_fetch(*args, **kwargs):
            call_count["n"] += 1
            return payload

        cache = CredentialCache()
        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            side_effect=_counted_fetch,
        ):
            result = cache.refresh_from_config("https://api.example.com", fetch_tokens=True)

        assert result == (1, 1)
        assert call_count["n"] == 1, (
            "refresh_from_config(fetch_tokens=True) should hit the server once, "
            f"not {call_count['n']} times — share the payload between views."
        )
        assert cache.tokens == {("anthropic", "default"): "tok"}

    def test_refresh_from_config_returns_cache_token_count_when_not_fetching(self):
        """When ``fetch_tokens=False``, no token fetch happens, but the
        cache may still hold tokens from an earlier call. The returned
        tuple's token count should describe the cache, not this call —
        otherwise dashboards and health checks that read the return
        value see a misleading 0 (PR #283 round-4 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            accounts={"anthropic": ["default"]},
            tokens={("anthropic", "default"): "tok-old"},
        )

        payload = {
            "config": {
                "providers": {
                    "anthropic": {"accounts": [{"account_id": "default", "enabled": True}]}
                }
            }
        }

        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            return_value=payload,
        ):
            account_count, token_count = cache.refresh_from_config(
                "https://api.example.com", fetch_tokens=False
            )

        # account count is what we just fetched; token count is the
        # cache state — still holding the pre-existing token even though
        # we didn't refetch.
        assert account_count == 1
        assert token_count == 1, (
            "refresh_from_config(fetch_tokens=False) must report the cache's "
            "token count, not a fresh-fetch-only count — callers see the cache "
            "not the network call."
        )
        assert cache.tokens == {("anthropic", "default"): "tok-old"}

    def test_provider_tag_hints_empty_by_default(self):
        """A freshly-constructed cache exposes an empty tag-hint view.
        ``GenericCollector.collect_provider`` consults this as a
        fallback before blocking a card (PR #288 / #290)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        assert cache.provider_tag_hints() == {}

    def test_refresh_from_config_populates_tag_hints(self):
        """``refresh_from_config`` now picks up the operator-resolved
        tag-hint map from the same payload (PR #290 round-2 review —
        earlier code only refreshed identity + tokens)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        payload = {
            "config": {
                "providers": {
                    "anthropic": {
                        "accounts": [{"account_id": "default", "enabled": True}],
                    }
                }
            },
            "account_tag_hints": {
                "anthropic": {"provider:anthropic": "alice@example.com"},
            },
        }

        cache = CredentialCache()
        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            return_value=payload,
        ):
            cache.refresh_from_config("https://api.example.com", fetch_tokens=False)

        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "alice@example.com"},
        }

    def test_refresh_from_config_keeps_prior_tag_hints_when_field_absent(self):
        """Older server (pre-PR #288) omits ``account_tag_hints``. The
        cache's prior hint map survives that round-trip — a downgrade
        doesn't silently wipe previously-cached hints (PR #290 round-2
        review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            tag_hints={"anthropic": {"provider:anthropic": "alice@example.com"}},
        )

        payload = {
            "config": {
                "providers": {
                    "anthropic": {"accounts": [{"account_id": "default", "enabled": True}]}
                }
            }
            # no ``account_tag_hints`` key — older server
        }

        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            return_value=payload,
        ):
            cache.refresh_from_config("https://api.example.com", fetch_tokens=False)

        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "alice@example.com"},
        }, "missing field must preserve the prior hint map, not clear it"

    def test_provider_tag_hints_returns_defensive_copy(self):
        """Mutating the returned map must NOT mutate the cache's
        internal state — same contract as ``provider_accounts``."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            tag_hints={"anthropic": {"provider:anthropic": "alice@example.com"}},
        )
        result = cache.provider_tag_hints()
        result["anthropic"]["provider:anthropic"] = "tampered@example.com"
        result["new_provider"] = {"provider:new": "evil@example.com"}
        # Re-fetch proves the cache is unchanged.
        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "alice@example.com"},
        }
