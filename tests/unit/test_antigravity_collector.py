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
