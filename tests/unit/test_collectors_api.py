"""Unit tests for API quota collectors (Gemini, Zai, OpenRouter, DeepSeek) and cross-collector invariants."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.collectors.deepseek import DeepSeekCollector
from app.services.collectors.gemini import GeminiCollector
from app.services.collectors.kimi_api import KimiApiCollector
from app.services.collectors.kimi_k2 import KimiK2Collector
from app.services.collectors.minimax import MiniMaxCollector
from app.services.collectors.openrouter import OpenRouterCollector
from app.services.collectors.zai import ZaiCollector
from tests.unit._collectors_helpers import _gemini_cache_with


@pytest.mark.parametrize(
    ("collector_cls", "provider_id", "module", "lookup", "env_key"),
    [
        (KimiApiCollector, "kimi_api", "kimi_api", "_get_current_key", "KIMI_API_KEY"),
        (KimiK2Collector, "kimi_k2", "kimi_k2", "_get_current_key", "KIMI_K2_API_KEY"),
        (MiniMaxCollector, "minimax", "minimax", "_get_current_creds", "MINIMAX_API_KEY"),
        (OpenRouterCollector, "openrouter", "openrouter", "_get_api_key", "OPENROUTER_API_KEY"),
        (ZaiCollector, "zai", "zai", "_get_api_key", "ZAI_API_KEY"),
    ],
)
class TestCredentialAccountSlotLookup:
    @pytest.mark.asyncio
    async def test_api_key_lookup_uses_credential_account_id(
        self, collector_cls, provider_id, module, lookup, env_key
    ):
        collector = collector_cls(account_id="alice@example.com")
        collector.credential_account_id = "default"
        settings_path = f"app.services.collectors.{module}.settings"
        credential_path = (
            f"app.services.collectors.{module}.credential_provider.get_provider_api_key"
        )
        cache_path = "app.services.token_cache.token_cache.get_with_metadata"

        with (
            patch(settings_path) as settings,
            patch(credential_path, return_value=None),
            patch(
                cache_path,
                new_callable=AsyncMock,
                return_value=(
                    {"api_key": "api-test-key"},  # pragma: allowlist secret
                    {"source": "sidecar"},
                ),
            ) as get_with_metadata,
        ):
            setattr(settings, env_key, "")
            resolved = await getattr(collector, lookup)()

        assert resolved == "api-test-key"
        get_with_metadata.assert_awaited_once_with(provider_id, account_id="default")


class TestGeminiCollector:
    """Test suite for Google Gemini collector."""

    @pytest.mark.asyncio
    async def test_collect_api_success(self, mock_http_client, mock_gemini_quota_response):
        """Test successful Gemini API collection with project discovery."""
        collector = GeminiCollector()

        # Mock responses - tier request comes FIRST (to get project ID)
        tier_response = MagicMock(spec=httpx.Response)
        tier_response.status_code = 200
        tier_response.headers = {}
        tier_response.json.return_value = {
            "currentTier": {"id": "standard-tier", "name": "Gemini Code Assist"},
            "cloudaicompanionProject": "test-project-123",
        }

        quota_response = MagicMock(spec=httpx.Response)
        quota_response.status_code = 200
        quota_response.headers = {}
        quota_response.json.return_value = mock_gemini_quota_response

        # Create async mock that returns responses in order
        call_count = [0]

        async def mock_request(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return tier_response  # First call: loadCodeAssist
            return quota_response  # Second call: retrieveUserQuota

        mock_http_client.request = mock_request

        cache = await _gemini_cache_with({"oauth_token": "token", "expiry_date": "9999999999999"})
        with patch("app.services.collectors.gemini_oauth.token_cache", cache):
            result = await collector.collect(mock_http_client)

        assert isinstance(result, list)
        assert len(result) >= 1
        # Should return one card per model family
        assert len(result) <= len(mock_gemini_quota_response["buckets"])
        # Check that service name contains model identifier (either display name or raw model ID)
        assert any(name in str(result[0].get("service_name", "")) for name in ["Gemini", "gemini"])
        # Verify health field exists
        assert "health" in result[0]
        # Verify unit is "used" (not "quota")
        assert result[0].get("unit") == "used"
        # Verify project was used in quota call
        assert call_count[0] == 2  # Should make 2 API calls

    @pytest.mark.asyncio
    async def test_collect_api_with_absolute_quota(self, mock_http_client):
        """Test Gemini API collection with absolute quota fields (quotaLimit, quotaRemaining)."""
        collector = GeminiCollector()

        # Mock responses
        tier_response = MagicMock(spec=httpx.Response)
        tier_response.status_code = 200
        tier_response.json.return_value = {
            "currentTier": {"id": "standard-tier", "name": "Gemini Code Assist"},
            "cloudaicompanionProject": "test-project-123",
        }

        quota_response = MagicMock(spec=httpx.Response)
        quota_response.status_code = 200
        quota_response.json.return_value = {
            "buckets": [
                {
                    "modelId": "gemini-1.5-pro",
                    "remainingFraction": 0.85,
                    "resetTime": "2025-04-08T00:00:00Z",
                    "quotaLimit": 1000,
                    "quotaRemaining": 850,
                    "tokenType": "REQUEST",
                }
            ]
        }

        async def mock_request(*args, **kwargs):
            if "loadCodeAssist" in args[1]:
                return tier_response
            return quota_response

        mock_http_client.request = mock_request

        cache = await _gemini_cache_with({"oauth_token": "token", "expiry_date": "9999999999999"})
        with patch("app.services.collectors.gemini_oauth.token_cache", cache):
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        card = result[0]
        # Primary remaining should still be %
        assert card["remaining"] == "15%"  # 100 - (0.85*100)
        # Detail should contain absolute numbers
        assert "850 / 1,000 request left" in card["detail"]
        # used_value and limit_value should be absolute
        assert card["used_value"] == 150.0  # 1000 - 850
        assert card["limit_value"] == 1000.0
        assert card["unit_type"] == "request"

    @pytest.mark.asyncio
    async def test_collect_missing_credentials(self, mock_http_client):
        """Test graceful handling when no sidecar/config bundle is cached."""
        collector = GeminiCollector()

        cache = await _gemini_cache_with(None)
        with patch("app.services.collectors.gemini_oauth.token_cache", cache):
            result = await collector.collect(mock_http_client)

        # Should return empty list or fallback to logs
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_collect_prefers_bucket_with_quota_over_fraction_only(self, mock_http_client):
        """When two buckets map to the same model class (e.g. preview + GA pro),
        the dedup must keep the one carrying concrete quotaLimit/quotaRemaining,
        not the fraction-only one that happened to come first in the response.
        """
        collector = GeminiCollector()

        tier_response = MagicMock(spec=httpx.Response)
        tier_response.status_code = 200
        tier_response.json.return_value = {
            "currentTier": {"id": "g1-pro-tier", "name": "Pro"},
            "cloudaicompanionProject": "test-project-123",
        }

        # Preview bucket (fraction-only, no quotaLimit) appears FIRST.
        # GA bucket (with quotaLimit) appears SECOND.
        # Both map to model_class="pro".
        quota_response = MagicMock(spec=httpx.Response)
        quota_response.status_code = 200
        quota_response.json.return_value = {
            "buckets": [
                {
                    "modelId": "gemini-2.5-pro-preview",
                    "remainingFraction": 1.0,
                    "resetTime": "2025-04-08T00:00:00Z",
                },
                {
                    "modelId": "gemini-2.5-pro",
                    "remainingFraction": 0.65,
                    "resetTime": "2025-04-08T00:00:00Z",
                    "quotaLimit": 5000,
                    "quotaRemaining": 3250,
                    "tokenType": "REQUEST",
                },
            ]
        }

        async def mock_request(*args, **kwargs):
            if "loadCodeAssist" in args[1]:
                return tier_response
            return quota_response

        mock_http_client.request = mock_request

        cache = await _gemini_cache_with({"oauth_token": "token", "expiry_date": "9999999999999"})
        with patch("app.services.collectors.gemini_oauth.token_cache", cache):
            result = await collector.collect(mock_http_client)

        # Exactly one Pro card; the GA bucket (with quotaLimit) wins
        pro_cards = [c for c in result if c.get("model_id") == "pro"]
        assert len(pro_cards) == 1
        pro = pro_cards[0]
        assert pro["limit_value"] == 5000.0
        assert pro["used_value"] == 1750.0  # 5000 - 3250
        assert "3,250 / 5,000 request left" in pro["detail"]


class TestZaiCollector:
    """Test suite for zAI (consolidated) collector."""

    @pytest.mark.asyncio
    async def test_collect_quota_success_token_limit(self, mock_http_client):
        """Test successful zAI plan collection with token limit."""
        collector = ZaiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "code": 200,
            "success": True,
            "data": {
                "planName": "Basic Plan",
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "unit": 3,
                        "number": 168,
                        "usage": 1000000,
                        "currentValue": 450000,
                        "remaining": 550000,
                        "percentage": 45,
                        "nextResetTime": 1775570736000,
                    }
                ],
            },
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_valid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) >= 1
        token_card = next((c for c in result if c.get("variant") == "Tokens"), None)
        assert token_card is not None
        assert "550,000" in token_card["remaining"]
        assert token_card["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_quota_success_both_limits(self, mock_http_client):
        """Test successful zAI plan collection with both token and time limits."""
        collector = ZaiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "code": 200,
            "success": True,
            "data": {
                "planName": "Pro Plan",
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "unit": 3,
                        "number": 168,
                        "usage": 1000000,
                        "currentValue": 200000,
                        "remaining": 800000,
                        "percentage": 20,
                        "nextResetTime": 1775570736000,
                    },
                    {
                        "type": "TIME_LIMIT",
                        "unit": 5,
                        "number": 43200,
                        "usage": 3600,
                        "currentValue": 900,
                        "remaining": 2700,
                        "percentage": 25,
                        "nextResetTime": 1775570736000,
                    },
                ],
            },
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_valid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) >= 2
        assert any(c.get("variant") == "Tokens" for c in result)
        assert any(c.get("variant") == "Time" for c in result)

    @pytest.mark.asyncio
    async def test_collect_no_plan_returns_info_card(self, mock_http_client):
        """Test zAI returns info card when no plan/limits available."""
        collector = ZaiCollector()

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "code": 200,
            "success": True,
            "data": {"limits": []},
        }

        mock_http_client.get.return_value = response

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_valid_key"
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "No active plan"
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_invalid_key_returns_info_card(self, mock_http_client):
        """Test zAI returns info card when key is placeholder."""
        collector = ZaiCollector()

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "zai"
            with patch(
                "app.services.collectors.zai.credential_provider.get_provider_api_key",
                return_value=None,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert "zAI" in result[0]["service_name"]
        assert result[0]["remaining"] == "No API key configured"
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_no_key_returns_info_card(self, mock_http_client):
        """Test zAI returns info card when no API key."""
        collector = ZaiCollector()

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = ""
            with patch(
                "app.services.collectors.zai.credential_provider.get_provider_api_key",
                return_value=None,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "No API key configured"
        assert result[0]["health"] == "good"


class TestOpenRouterCollector:
    """Test suite for OpenRouter collector."""

    @pytest.mark.asyncio
    async def test_collect_success(self, mock_http_client):
        """Test successful OpenRouter credits-only collection."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 200
            key_resp.json.return_value = {"data": {"limit": None, "usage": 0.0}}

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    return_value=key_resp,
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "OpenRouter"
        assert result[0].get("variant") == "Credits"
        assert "$7.50" in result[0]["remaining"]
        assert result[0]["health"] == "good"

    @pytest.mark.asyncio
    async def test_collect_success_with_key_limit(self, mock_http_client):
        """Test collection returns two cards when key endpoint has a spending limit."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 200
            key_resp.json.return_value = {"data": {"limit": 20.0, "usage": 0.5}}

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    return_value=key_resp,
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 2
        services = {c.get("variant"): c for c in result}
        assert "Credits" in services
        assert "Key Limit" in services
        key_card = services["Key Limit"]
        assert "$19.50" in key_card["remaining"]
        assert key_card["health"] == "good"
        assert key_card["limit_value"] == 20.0
        assert key_card["used_value"] == 0.5

    @pytest.mark.asyncio
    async def test_collect_key_endpoint_failure_graceful(self, mock_http_client):
        """Test that key endpoint failure still returns the credits card."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 500
            key_resp.text = "Internal Server Error"

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    return_value=key_resp,
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "OpenRouter"
        assert result[0].get("variant") == "Credits"

    @pytest.mark.asyncio
    async def test_collect_key_timeout_still_returns_credits(self, mock_http_client):
        """Test that key endpoint timeout still returns the credits card."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    side_effect=httpx.TimeoutException("timeout"),
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "OpenRouter"
        assert result[0].get("variant") == "Credits"

    @pytest.mark.asyncio
    async def test_collect_key_no_limit_configured(self, mock_http_client):
        """Test that key endpoint with no limit configured only returns credits card."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 200
            key_resp.json.return_value = {"data": {"limit": None, "usage": 0.0}}

            with (
                patch(
                    "app.services.collectors.openrouter.http_request_with_retry",
                    new_callable=AsyncMock,
                    return_value=credits_resp,
                ),
                patch.object(
                    collector,
                    "_key_endpoint_request",
                    new_callable=AsyncMock,
                    return_value=key_resp,
                ),
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["service_name"] == "OpenRouter"
        assert result[0].get("variant") == "Credits"

    @pytest.mark.asyncio
    async def test_headers_include_referer_and_title(self, mock_http_client):
        """Test that HTTP-Referer and X-Title headers are sent when configured."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = "https://example.com"
            mock_settings.OPENROUTER_X_TITLE = "MyApp"
            collector = OpenRouterCollector()

            credits_resp = MagicMock(spec=httpx.Response)
            credits_resp.status_code = 200
            credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

            key_resp = MagicMock(spec=httpx.Response)
            key_resp.status_code = 500
            key_resp.text = "error"

            mock_http_client.get.side_effect = [credits_resp, key_resp]
            await collector.collect(mock_http_client)

        calls = mock_http_client.get.call_args_list
        for call in calls:
            headers = call.kwargs.get("headers", {})
            assert headers.get("HTTP-Referer") == "https://example.com"
            assert headers.get("X-Title") == "MyApp"

    @pytest.mark.asyncio
    async def test_collect_api_error(self, mock_http_client):
        """Test OpenRouter collection with credits API error."""
        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "or_valid_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            collector = OpenRouterCollector()

            response = MagicMock(spec=httpx.Response)
            response.status_code = 500
            response.text = "Internal Server Error"
            mock_http_client.get.return_value = response

            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert "API connection failed" in result[0]["detail"]


class TestDeepSeekCollector:
    """Test suite for the DeepSeek prepaid-balance collector."""

    @pytest.mark.asyncio
    async def test_collect_success(self, mock_http_client):
        """Balance card with the paid/granted split in detail."""
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                return_value="sk-deepseek",
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = ""
            collector = DeepSeekCollector()

            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "USD",
                        "total_balance": "50.00",
                        "granted_balance": "10.00",
                        "topped_up_balance": "40.00",
                    }
                ],
            }

            with patch(
                "app.services.collectors.deepseek.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        card = result[0]
        assert card["service_name"] == "DeepSeek"
        assert card.get("variant") == "Balance"
        assert card["remaining"] == "$50.00"
        assert card["unit"] == "USD"
        assert card["unit_type"] == "currency"
        assert card["reset"] == "Prepaid"
        assert card["health"] == "good"
        assert "Paid: $40.00 / Granted: $10.00" in card["detail"]
        assert card["data_source"] == "api"

    @pytest.mark.asyncio
    async def test_collect_prefers_usd_across_currencies(self, mock_http_client):
        """When multiple currencies come back, the USD entry wins."""
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                return_value="sk-deepseek",
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = ""
            collector = DeepSeekCollector()

            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "100.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "100.00",
                    },
                    {
                        "currency": "USD",
                        "total_balance": "7.50",
                        "granted_balance": "2.50",
                        "topped_up_balance": "5.00",
                    },
                ],
            }

            with patch(
                "app.services.collectors.deepseek.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "$7.50"
        assert result[0]["unit"] == "USD"

    @pytest.mark.asyncio
    async def test_collect_unavailable_balance_degrades_health(self, mock_http_client):
        """is_available=false keeps the card but flags it and explains why."""
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                return_value="sk-deepseek",
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = ""
            collector = DeepSeekCollector()

            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "is_available": False,
                "balance_infos": [
                    {
                        "currency": "USD",
                        "total_balance": "50.00",
                        "granted_balance": "10.00",
                        "topped_up_balance": "40.00",
                    }
                ],
            }

            with patch(
                "app.services.collectors.deepseek.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["health"] == "warning"
        assert "balance unavailable for API calls" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_collect_zero_balance_is_critical(self, mock_http_client):
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                return_value="sk-deepseek",
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = ""
            collector = DeepSeekCollector()

            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "USD",
                        "total_balance": "0.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "0.00",
                    }
                ],
            }

            with patch(
                "app.services.collectors.deepseek.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["health"] == "critical"

    @pytest.mark.asyncio
    async def test_collect_api_error_returns_error_card(self, mock_http_client):
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                return_value="sk-deepseek",
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = ""
            collector = DeepSeekCollector()

            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 500
            resp.text = "Internal Server Error"

            with patch(
                "app.services.collectors.deepseek.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ):
                result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert "API connection failed" in result[0]["detail"]

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_error_card(self, mock_http_client):
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                return_value=None,
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = ""
            collector = DeepSeekCollector()

            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "ERR"
        assert result[0].get("error_type") == "missing_config"

    @pytest.mark.asyncio
    async def test_scoped_db_read_finds_hashed_account_row(self, mock_http_client):
        """A wizard-pasted key is stored under a hashed account_id, which the
        legacy unscoped read hides — the collector must ask for its own row."""
        seen: list[str | None] = []

        def lookup(_pid: str, *, account_id: str | None = None) -> str | None:
            seen.append(account_id)
            return (
                "sk-hashed" if account_id == "wizard-account" else None
            )  # pragma: allowlist secret

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "1.00",
                    "granted_balance": "0.00",
                    "topped_up_balance": "1.00",
                }
            ],
        }
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                side_effect=lookup,
            ),
            patch(
                "app.services.collectors.deepseek.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = ""
            collector = DeepSeekCollector(account_id="wizard-account")
            result = await collector.collect(mock_http_client)

        assert seen[0] == "wizard-account"
        assert len(result) == 1
        assert result[0]["remaining"] == "$1.00"
        assert result[0]["input_source"] == "config"

    @pytest.mark.asyncio
    async def test_cache_oauth_token_is_a_usable_key(self, mock_http_client):
        """The DB→cache mirror writes the key into oauth_token; a row that
        only has that slot must still configure the collector."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "2.00",
                    "granted_balance": "0.00",
                    "topped_up_balance": "2.00",
                }
            ],
        }
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.collectors.deepseek.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=({"oauth_token": "sk-from-cache"}, {"source": "config"}),
            ),
            patch(
                "app.services.collectors.deepseek.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = ""
            collector = DeepSeekCollector(account_id="wizard-account")
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "$2.00"
        assert result[0]["input_source"] == "config"

    @pytest.mark.asyncio
    async def test_cache_row_without_key_falls_through_to_env(self, mock_http_client):
        """A cache row missing both key fields is not terminal — the env
        default still configures the default-account collector."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "3.00",
                    "granted_balance": "0.00",
                    "topped_up_balance": "3.00",
                }
            ],
        }
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.collectors.deepseek.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=({"session_cookie": "not-a-key"}, {"source": "sidecar"}),
            ),
            patch(
                "app.services.collectors.deepseek.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=resp,
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = "sk-from-env"  # pragma: allowlist secret
            collector = DeepSeekCollector(account_id="default")
            result = await collector.collect(mock_http_client)

        assert len(result) == 1
        assert result[0]["remaining"] == "$3.00"
        assert result[0]["input_source"] == "server"

    @pytest.mark.asyncio
    async def test_sidecar_account_reads_own_cache_not_default_row(self, mock_http_client):
        """A sidecar-spawned account with no ProviderConfig row must not adopt
        the legacy default-row key — that skips its own cache entry."""
        seen: list[str | None] = []

        def lookup(_pid: str, *, account_id: str | None = None) -> str | None:
            seen.append(account_id)
            return "sk-default-row" if account_id == "default" else None  # pragma: allowlist secret

        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "4.00",
                    "granted_balance": "0.00",
                    "topped_up_balance": "4.00",
                }
            ],
        }
        cache = AsyncMock(return_value=({"api_key": "sk-sidecar"}, {"source": "sidecar"}))
        request = AsyncMock(return_value=resp)
        with (
            patch("app.services.collectors.deepseek.settings") as mock_settings,
            patch(
                "app.services.collectors.deepseek.credential_provider.get_provider_api_key",
                side_effect=lookup,
            ),
            patch(
                "app.services.collectors.deepseek.token_cache.get_with_metadata",
                cache,
            ),
            patch(
                "app.services.collectors.deepseek.http_request_with_retry",
                request,
            ),
        ):
            mock_settings.DEEPSEEK_API_KEY = "sk-from-env"  # pragma: allowlist secret
            collector = DeepSeekCollector(account_id="sidecar-host")
            result = await collector.collect(mock_http_client)

        cache.assert_awaited()
        assert "default" not in seen
        assert request.await_args.kwargs["headers"]["Authorization"] == "Bearer sk-sidecar"
        assert len(result) == 1
        assert result[0]["input_source"] == "sidecar"


class TestHttpTimeouts:
    """I2: All collector HTTP calls must pass an explicit timeout."""

    @pytest.mark.asyncio
    async def test_github_collector_passes_timeout(self, mock_http_client):
        """GitHub collector must pass timeout= on all HTTP calls."""
        from app.services.collectors.github import GitHubCollector

        collector = GitHubCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"assignee": {"chat_enabled": True}}
        mock_http_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.github.credential_provider") as mock_cp:
            fake_token = "ghp_test123"  # pragma: allowlist secret
            mock_cp.get_github_data.return_value = {"api_key": fake_token}
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert "timeout" in call.kwargs, f"GitHub HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_zai_collector_passes_timeout(self, mock_http_client):
        """ZAI collector must pass timeout= on its HTTP call."""
        from app.services.collectors.zai import ZaiCollector

        collector = ZaiCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"available_balance": "10.0"}}
        mock_http_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.zai.settings") as mock_settings:
            mock_settings.ZAI_API_KEY = "test_key"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert "timeout" in call.kwargs, f"ZAI HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_kimi_coding_collector_passes_timeout(self, mock_http_client):
        """Kimi Coding collector must pass timeout= on its HTTP call."""
        from app.services.collectors.kimi_coding import KimiCodingCollector

        collector = KimiCodingCollector()

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_http_client.post = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.kimi_coding.settings") as mock_settings:
            mock_settings.KIMI_AUTH_TOKEN = "test_kimi_key"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.post.call_args_list):
            assert "timeout" in call.kwargs, f"Kimi Coding HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_openrouter_collector_passes_timeout(self, mock_http_client):
        """OpenRouter collector must pass timeout= on its HTTP call."""
        collector = OpenRouterCollector()
        credits_resp = MagicMock(spec=httpx.Response)
        credits_resp.status_code = 200
        credits_resp.json.return_value = {"data": {"total_credits": 10.0, "usage": 2.5}}

        key_resp = MagicMock(spec=httpx.Response)
        key_resp.status_code = 200
        key_resp.json.return_value = {"data": {"limit": None, "usage": 0.0}}

        mock_http_client.get = AsyncMock(side_effect=[credits_resp, key_resp])

        with patch("app.services.collectors.openrouter.settings") as mock_settings:
            mock_settings.OPENROUTER_API_KEY = "test_key"
            mock_settings.OPENROUTER_HTTP_REFERER = ""
            mock_settings.OPENROUTER_X_TITLE = "Runway"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert "timeout" in call.kwargs, f"OpenRouter HTTP call #{i} missing timeout=: {call}"

    @pytest.mark.asyncio
    async def test_minimax_collector_passes_timeout(self, mock_http_client):
        """MiniMax collector must pass timeout= on its HTTP call."""
        collector = MiniMaxCollector()
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"model_remains": []}
        mock_http_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.collectors.minimax.settings") as mock_settings:
            mock_settings.MINIMAX_API_KEY = "test_key"
            await collector.collect(mock_http_client)

        for i, call in enumerate(mock_http_client.get.call_args_list):
            assert "timeout" in call.kwargs, f"MiniMax HTTP call #{i} missing timeout=: {call}"
