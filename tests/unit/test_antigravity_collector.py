"""Unit tests for the Antigravity collector token lookup and error handling.

Covers the durable-identity / hash-key mismatch fix: the agy token file carries
no id_token, so the sidecar-pushed token is cached under a refresh-token-derived
hash, while the collector's account_id is seeded with the email from LatestUsage.
The collector must still find the token (newest-entry fallback) without adopting
the hash, and must surface an auth error card when the token is rejected.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.collectors.antigravity import AntigravityCollector
from app.services.token_cache import token_cache


class TestAntigravityTokenLookup:
    @pytest.mark.asyncio
    async def test_falls_back_to_newest_entry_on_account_mismatch(self):
        """account_id=email misses the hash-keyed cache entry → fall back to newest."""
        await token_cache.reset()
        try:
            # Sidecar push with no account_id → cached under a refresh-token hash.
            await token_cache.store(
                "antigravity",
                {"oauth_token": "ya29.live", "refresh_token": "1//refresh"},
                account_id=None,
                source="dev-01",
            )

            # Collector seeded with the email (durable identity from LatestUsage),
            # which is NOT a cache key and has no "default" entry to catch it.
            collector = AntigravityCollector(account_id="user@example.com")
            token = await collector._get_current_token()

            assert token == "ya29.live"
            # Identity preserved — must NOT be overwritten with the hash.
            assert collector.account_id == "user@example.com"
        finally:
            await token_cache.reset()

    @pytest.mark.asyncio
    async def test_returns_none_when_no_antigravity_tokens_cached(self):
        """No cache entry at all → None (is_configured stays False)."""
        await token_cache.reset()
        try:
            collector = AntigravityCollector(account_id="user@example.com")
            assert await collector._get_current_token() is None
        finally:
            await token_cache.reset()

    @pytest.mark.asyncio
    async def test_prefers_newest_non_expired_entry_over_expired_email_hit(self):
        """The email-keyed entry can be a HIT but still expired — e.g. two
        sidecars push tokens for the same account (one host's agy session
        lapsed, another's is live), and each overwrites the single shared
        email-keyed slot. A present-but-expired hit must not short-circuit
        the fallback; the newest non-expired entry (any key) should be used
        instead.

        This is the root-cause incident: dev-01's expired agy token kept
        clobbering mgmt's valid one under the same email key, and the
        collector accepted the expired hit without ever looking further.
        """
        await token_cache.reset()
        try:
            import time

            now_ms = int(time.time() * 1000)
            # mgmt's valid push, stored under a different (hash) key.
            await token_cache.store(
                "antigravity",
                {"oauth_token": "ya29.valid", "expiry_date": str(now_ms + 3_600_000)},
                account_id=None,
                source="sidecar-mgmt",
            )
            # dev-01's expired push, stored under the email key — a HIT, not a miss.
            await token_cache.store(
                "antigravity",
                {"oauth_token": "ya29.expired", "expiry_date": str(now_ms - 3_600_000)},
                account_id="user@example.com",
                source="sidecar-dev-01",
            )

            collector = AntigravityCollector(account_id="user@example.com")
            token = await collector._get_current_token()

            assert token == "ya29.valid"
            assert collector.account_id == "user@example.com"
        finally:
            await token_cache.reset()


class TestAntigravityErrorCards:
    @pytest.mark.asyncio
    async def test_loadcodeassist_401_returns_auth_failed_card(self, mock_http_client):
        """A 401 from loadCodeAssist surfaces an auth_failed card, not []."""
        collector = AntigravityCollector(account_id="user@example.com")

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.headers = {}

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.expired"
            ),
            patch(
                "app.services.collectors.antigravity_api.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            result = await collector._collect_via_api(mock_http_client)

        assert len(result) == 1
        assert result[0].get("error_type") == "auth_failed"

    @pytest.mark.asyncio
    async def test_email_account_id_populates_label(self, mock_http_client):
        """account_id=email with no label → label mirrors the email (not 'Default')."""
        collector = AntigravityCollector(account_id="user@example.com")
        assert collector.account_label in (None, "Default")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {"cloudaicompanionProject": "proj-1"}
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = {"groups": []}

        with (
            patch.object(collector, "_get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
        ):
            await collector._collect_via_api(mock_http_client)

        assert collector.account_label == "user@example.com"

    @pytest.mark.asyncio
    async def test_loadcodeassist_500_returns_api_error_card(self, mock_http_client):
        """A non-auth non-200 surfaces a generic api_error card, not []."""
        collector = AntigravityCollector(account_id="user@example.com")

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 500
        resp.headers = {}

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch(
                "app.services.collectors.antigravity_api.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            result = await collector._collect_via_api(mock_http_client)

        assert len(result) == 1
        assert result[0].get("error_type") == "api_error"


class TestAntigravityTier:
    @pytest.mark.asyncio
    async def test_collect_via_api_extracts_tier_from_paid_tier(self, mock_http_client):
        """Tier is extracted from paidTier in loadCodeAssist response and added to cards."""
        collector = AntigravityCollector(account_id="user@example.com")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {
            "cloudaicompanionProject": "proj-1",
            "paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro"},
        }

        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "g_weekly",
                            "window": "weekly",
                            "remainingFraction": 0.5,
                            "displayName": "Weekly",
                            "resetTime": "2026-08-04T00:00:00Z",
                        },
                        {
                            "bucketId": "g_5h",
                            "window": "5h",
                            "remainingFraction": 0.8,
                            "displayName": "Rolling",
                        },
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {
                            "bucketId": "f_weekly",
                            "window": "weekly",
                            "remainingFraction": 0.9,
                            "displayName": "Weekly",
                            "resetTime": "2026-08-04T00:00:00Z",
                        },
                    ],
                },
            ]
        }

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
        ):
            result = await collector._collect_via_api(mock_http_client)

        assert len(result) == 3
        for card in result:
            assert card.get("tier") == "pro", (
                f"Expected tier='pro' in card {card.get('service_name')}"
            )


def _quota_summary_response() -> dict:
    """A quota summary with all three pools the API serves (2026-10-20 shape).

    Values are taken from a live `retrieveUserQuotaSummary` answer: the Gemini
    pool is nearly exhausted, the Claude/GPT pool is untouched, and the paid
    "Gemini 4 Argon" pool has its own separate weekly limit.
    """
    return {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "window": "weekly",
                        "remainingFraction": 0.0032,
                        "displayName": "Weekly Limit Remaining",
                        "resetTime": "2026-10-23T21:59:41Z",
                    },
                    {
                        "bucketId": "gemini-5h",
                        "window": "5h",
                        "remainingFraction": 0.9909,
                        "displayName": "Five Hour Limit Remaining",
                        "resetTime": "2026-10-20T11:01:40Z",
                    },
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "bucketId": "3p-weekly",
                        "window": "weekly",
                        "remainingFraction": 1,
                        "displayName": "Weekly Limit Remaining",
                        "resetTime": "2026-10-27T06:19:20Z",
                    },
                    {
                        "bucketId": "3p-5h",
                        "window": "5h",
                        "remainingFraction": 1,
                        "displayName": "Five Hour Limit Remaining",
                        "resetTime": "2026-10-20T11:19:20Z",
                    },
                ],
            },
            {
                "displayName": "Gemini 4 Argon",
                "description": "For paid Pro plans, Argon quota is separate.",
                "buckets": [
                    {
                        "bucketId": "gemini-argon-wtu-weekly",
                        "window": "weekly",
                        "remainingFraction": 0.8247,
                        "displayName": "Gemini 4 Argon Weekly Limit",
                        "resetTime": "2026-10-27T05:52:07Z",
                    },
                ],
            },
        ]
    }


class TestAntigravityQuotaPools:
    """The API answers with one group per *physical* quota pool. Each pool
    needs its own `variant`, because LatestUsage keys a row on
    (provider, account, window_type, variant, model_id) — two pools sharing a
    variant would overwrite each other instead of showing up side by side.
    """

    @pytest.mark.asyncio
    async def test_three_pools_yield_five_cards_with_distinct_slots(self, mock_http_client):
        collector = AntigravityCollector(account_id="user@example.com")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {"cloudaicompanionProject": "aicode-consumers"}
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = _quota_summary_response()

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
        ):
            result = await collector._collect_via_api(mock_http_client)

        assert len(result) == 5
        # Every card sits in its own LatestUsage slot.
        slots = {(c["window_type"], c["variant"]) for c in result}
        assert slots == {
            ("weekly", "gemini"),
            ("session", "gemini"),
            ("weekly", "frontier"),
            ("session", "frontier"),
            ("weekly", "argon"),
        }
        assert len({c["quota_pool_id"] for c in result}) == 5

    @pytest.mark.asyncio
    async def test_argon_group_is_its_own_pool(self, mock_http_client):
        """ "Gemini 4 Argon" says "gemini" but is a separate paid-plan pool —
        it must not land in the gemini pool's slot."""
        collector = AntigravityCollector(account_id="user@example.com")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {"cloudaicompanionProject": "aicode-consumers"}
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = _quota_summary_response()

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
        ):
            result = await collector._collect_via_api(mock_http_client)

        argon = [c for c in result if c["variant"] == "argon"]
        assert len(argon) == 1
        card = argon[0]
        assert card["service_name"] == "Argon"
        assert card["window_type"] == "weekly"
        assert card["quota_pool_id"] == "antigravity:argon:weekly"
        # 1 - 0.8247 remaining → 17.53 % used.
        assert card["pct_used"] == 17.53
        assert card["reset_at"] == "2026-10-27T05:52:07+00:00"
        assert "Gemini 4 Argon" in card["detail"]

    @pytest.mark.asyncio
    async def test_five_hour_window_maps_to_session(self, mock_http_client):
        """The API's "5h" window is the canonical "session" window."""
        collector = AntigravityCollector(account_id="user@example.com")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {"cloudaicompanionProject": "aicode-consumers"}
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = _quota_summary_response()

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
        ):
            result = await collector._collect_via_api(mock_http_client)

        gemini_weekly = next(
            c for c in result if c["variant"] == "gemini" and c["window_type"] == "weekly"
        )
        assert gemini_weekly["pct_used"] == 99.68
        assert gemini_weekly["quota_pool_id"] == "antigravity:gemini:weekly"
        gemini_session = next(
            c for c in result if c["variant"] == "gemini" and c["window_type"] == "session"
        )
        assert gemini_session["quota_pool_id"] == "antigravity:gemini:session"

    def test_pool_family_maps_group_names(self):
        from app.services.collectors.antigravity_api import pool_family

        assert pool_family("Gemini Models") == "gemini"
        assert pool_family("Claude and GPT models") == "frontier"
        assert pool_family("Gemini 4 Argon") == "argon"
        # A version bump keeps the Argon pool (the pattern is anchored, so it
        # cannot swallow a different pool).
        assert pool_family("Gemini 5 Argon") == "argon"

    def test_a_new_pool_named_like_a_known_one_does_not_join_it(self):
        """Loose substring matching would fold a new pool into a known one —
        and two pools sharing a variant share a latest_usage row, where the
        second one is silently dropped."""
        from app.services.collectors.antigravity_api import POOL_LABELS, pool_family

        for name in ("Gemini Pro Models", "GPT-OSS models", "Claude Opus models"):
            pool = pool_family(name)
            assert pool not in POOL_LABELS, f"{name!r} was folded into {pool!r}"
        assert pool_family("Gemini Pro Models").startswith("gemini-pro-models-")

    def test_a_bare_known_name_does_not_take_a_live_pool_key(self):
        """A group called "Argon" misses the anchored patterns and slugs to
        "argon" — the live Argon pool's key. Reserved keys stay reserved."""
        from app.services.collectors.antigravity_api import POOL_LABELS, pool_family

        for name in ("Argon", "Gemini", "Frontier"):
            pool = pool_family(name)
            assert pool not in POOL_LABELS, f"{name!r} took the live pool key {pool!r}"
            assert pool.startswith("unknown-")

    def test_two_groups_differing_only_in_case_stay_apart(self):
        """The slug folds case and punctuation — the hash keeps such groups
        apart, so they cannot share a latest_usage row."""
        from app.services.collectors.antigravity_api import pool_family

        assert pool_family("Gemini 5 Code") != pool_family("gemini 5-code")
        assert pool_family("Gemini 5 Code").startswith("gemini-5-code-")

    def test_unknown_group_gets_its_own_pool(self):
        """An unrecognised group must not land in an existing pool: two pools
        sharing a variant share a latest_usage slot and overwrite each other."""
        from app.services.collectors.antigravity_api import POOL_LABELS, pool_family

        unknown = pool_family("Some Future Pool")
        assert unknown not in POOL_LABELS
        assert unknown.startswith("some-future-pool-")
        # …and it stays distinct from the known pools.
        assert unknown not in {pool_family("Gemini Models"), pool_family("Gemini 4 Argon")}

    @pytest.mark.asyncio
    async def test_unknown_group_is_reported_and_warned_about(self, mock_http_client, caplog):
        """An unknown pool is surfaced (own variant, own card) and logged."""
        import logging

        from app.services.collectors import antigravity_api

        # The unknown-pool warning is logged once per pool per process.
        antigravity_api._WARNED_UNKNOWN_POOLS.clear()
        collector = AntigravityCollector(account_id="user@example.com")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {"cloudaicompanionProject": "aicode-consumers"}
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = {
            "groups": [
                {
                    "displayName": "Some Future Pool",
                    "buckets": [
                        {
                            "bucketId": "future-weekly",
                            "window": "weekly",
                            "remainingFraction": 0.5,
                            "displayName": "Weekly Limit Remaining",
                        },
                    ],
                },
            ]
        }

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
            caplog.at_level(logging.WARNING),
        ):
            result = await collector._collect_via_api(mock_http_client)

        assert len(result) == 1
        assert result[0]["variant"].startswith("some-future-pool-")
        assert result[0]["quota_pool_id"].startswith("antigravity:some-future-pool-")
        assert result[0]["service_name"] == "Some Future Pool"
        assert any("Unknown Antigravity quota group" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unknown_paid_tier_still_warns_about_a_missing_gemini_pool(
        self, mock_http_client, caplog
    ):
        """The guard keys on the tier being known at all — a tier id Google
        adds later must not slip through an allow-list of known paid tiers."""
        import logging

        from app.services.collectors import antigravity_api

        antigravity_api._WARNED_MISSING_GEMINI_TIERS.clear()
        collector = AntigravityCollector(account_id="user@example.com")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {
            "cloudaicompanionProject": "aicode-consumers",
            "paidTier": {"id": "g1-max-tier"},
        }
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = {
            "groups": [
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 1},
                    ],
                },
            ]
        }

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
            caplog.at_level(logging.WARNING),
        ):
            await collector._collect_via_api(mock_http_client)

        assert any("no Gemini pool" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_free_tier_only_debugs_a_missing_gemini_pool(self, mock_http_client, caplog):
        """The User-Agent gate is account-independent, so a free account loses
        its Gemini card the same way — but that is only worth a debug line."""
        import logging

        from app.services.collectors import antigravity_api

        antigravity_api._WARNED_MISSING_GEMINI_TIERS.clear()
        collector = AntigravityCollector(account_id="user@example.com")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {
            "cloudaicompanionProject": "aicode-consumers",
            "currentTier": {"id": "free-tier"},
        }
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = {
            "groups": [
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 1},
                    ],
                },
            ]
        }

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
            caplog.at_level(logging.DEBUG),
        ):
            result = await collector._collect_via_api(mock_http_client)

        assert result[0]["tier"] == "free"
        assert any("no Gemini pool" in r.getMessage() for r in caplog.records)
        # …and it is not a warning (it would spam on every free account).
        assert not any(
            r.levelno == logging.WARNING and "no Gemini pool" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_unmapped_pool_keeps_the_api_spelling_of_its_name(self, mock_http_client):
        """The card name keeps the API's own spelling — a title-cased slug
        would mangle acronyms ("Claude & GPT models" → "Claude Gpt Models")."""
        from app.services.collectors import antigravity_api

        antigravity_api._WARNED_UNKNOWN_POOLS.clear()
        collector = AntigravityCollector(account_id="user@example.com")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {"cloudaicompanionProject": "aicode-consumers"}
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = {
            "groups": [
                {
                    "displayName": "Claude & GPT models",
                    "buckets": [
                        {"bucketId": "3p2-weekly", "window": "weekly", "remainingFraction": 1},
                    ],
                },
            ]
        }

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
        ):
            result = await collector._collect_via_api(mock_http_client)

        assert result[0]["service_name"] == "Claude & GPT models"

    @pytest.mark.asyncio
    async def test_group_without_a_display_name_keeps_its_own_pool(self, mock_http_client):
        """Two unnamed groups would both fall back to "Unknown" and overwrite
        each other — the bucket ids make a safe fallback."""

        collector = AntigravityCollector(account_id="user@example.com")

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {"cloudaicompanionProject": "aicode-consumers"}
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = {
            "groups": [
                {
                    "buckets": [
                        {"bucketId": "7p-weekly", "window": "weekly", "remainingFraction": 1},
                    ],
                },
                {
                    "buckets": [
                        {"bucketId": "9p-weekly", "window": "weekly", "remainingFraction": 1},
                    ],
                },
            ]
        }

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch.object(collector, "_ag_post", new_callable=AsyncMock, side_effect=[lca, qs]),
        ):
            result = await collector._collect_via_api(mock_http_client)

        assert len(result) == 2
        assert len({c["variant"] for c in result}) == 2
        assert {c["variant"].split("-")[0] for c in result} == {"7p", "9p"}
        # A group the API sent unnamed is labelled from its pool key, not
        # from a raw bucket id.
        labels = {c["service_name"] for c in result}
        assert labels == {"7P Weekly", "9P Weekly"}


class TestAntigravityUserAgent:
    """The UA version decides *which quota pools* the backend serves — an
    outdated one answers 200 with a subset (the Claude/GPT pool only) instead
    of failing loudly."""

    def test_user_agent_keeps_the_agy_prefix(self):
        """The prefix is the 403 gate: Google's ESF rejects anything else."""
        from app.services.collectors.antigravity_api import _USER_AGENT

        assert _USER_AGENT.startswith("antigravity/cli/")

    def test_user_agent_version_unlocks_every_pool(self):
        """< 1.2 serves only the Claude/GPT pool, >= 1.3 serves all three."""
        import re

        from app.services.collectors.antigravity_api import _USER_AGENT

        match = re.search(r"antigravity/cli/(\d+)\.(\d+)\.(\d+)", _USER_AGENT)
        assert match, f"cannot read a version out of {_USER_AGENT!r}"
        version = tuple(int(part) for part in match.groups())
        assert version >= (1, 3, 0), (
            f"UA version {version} hides the Gemini/Argon pools — bump _USER_AGENT"
        )

    @pytest.mark.asyncio
    async def test_quota_requests_send_the_agy_user_agent(self, mock_http_client):
        collector = AntigravityCollector(account_id="user@example.com")
        from app.services.collectors.antigravity_api import _USER_AGENT

        lca = MagicMock(spec=httpx.Response)
        lca.status_code = 200
        lca.json.return_value = {"cloudaicompanionProject": "aicode-consumers"}
        qs = MagicMock(spec=httpx.Response)
        qs.status_code = 200
        qs.json.return_value = _quota_summary_response()

        with (
            patch.object(
                collector, "_get_valid_token", new_callable=AsyncMock, return_value="ya29.live"
            ),
            patch(
                "app.services.collectors.antigravity_api.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[lca, qs],
            ) as post,
        ):
            await collector._collect_via_api(mock_http_client)

        assert post.await_count == 2
        for call in post.await_args_list:
            assert call.kwargs["headers"]["User-Agent"] == _USER_AGENT


class TestAntigravityNoServerSideRefresh:
    @pytest.mark.asyncio
    async def test_force_refresh_path_never_claims_a_server_side_refresh(self, caplog):
        """A 401 makes the collector re-read the cache (another host's push may
        be live), but the server cannot rotate agy's login — it must not log
        `Refreshing … access token` as if a refresh happened."""
        await token_cache.reset()
        try:
            import logging
            import time

            await token_cache.store(
                "antigravity",
                {
                    "oauth_token": "ya29.current",
                    "expiry_date": str(int(time.time() * 1000) + 3_600_000),
                },
                account_id="user@example.com",
                source="mgmt",
            )
            collector = AntigravityCollector(account_id="user@example.com")
            assert collector.REFRESHABLE is False
            refresh_attempted = AsyncMock(return_value=None)
            collector._execute_refresh = refresh_attempted

            with caplog.at_level(logging.DEBUG):
                token = await collector._get_valid_token(MagicMock(), force_refresh=True)

            # Cache re-read succeeded; nothing was rotated.
            assert token == "ya29.current"
            messages = [record.getMessage() for record in caplog.records]
            assert not any(message.startswith("Refreshing ") for message in messages)
            assert any("refresh skipped" in message for message in messages)
            # The contract: no client_id → the base never even attempts a refresh.
            refresh_attempted.assert_not_awaited()
        finally:
            await token_cache.reset()

    def test_refreshable_defaults_true_for_providers_that_can_rotate(self):
        from app.services.collectors.oauth_base import OAuthBaseCollector

        assert OAuthBaseCollector.REFRESHABLE is True
