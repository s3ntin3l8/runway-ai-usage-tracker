"""Smoke tests for Gemini collectors."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.collectors.gemini import GeminiCollector


@pytest.fixture
def gemini_api_response():
    return {
        "currentTier": "STANDARD",
        "quotaStatus": {
            "currentUsage": [
                {
                    "model": "gemini-2.5-flash",
                    "quotaBuckets": [
                        {
                            "bucket": "MONTHLY",
                            "quotaRemaining": 800,
                            "quotaLimit": 1000,
                            "tokenType": "units",
                            "resetAt": "2026-05-01T00:00:00Z",
                        }
                    ],
                }
            ]
        },
        "userEmail": "user@example.com",
    }


@pytest.mark.asyncio
async def test_gemini_api_no_token_returns_empty():
    """When no valid token is available, api strategy returns empty list."""
    collector = GeminiCollector()

    with patch.object(collector, "_get_valid_token", AsyncMock(return_value=None)):
        client = AsyncMock(spec=httpx.AsyncClient)
        result = await collector._collect_via_api(client)
    assert result == []


@pytest.mark.asyncio
async def test_gemini_api_success(gemini_api_response):
    """Valid API response produces quota cards."""
    collector = GeminiCollector()

    tier_resp = MagicMock()
    tier_resp.status_code = 200
    tier_resp.json.return_value = gemini_api_response

    with patch.object(collector, "_get_valid_token", AsyncMock(return_value="tok")):
        with patch(
            "app.services.collectors.gemini_api.http_request_with_retry",
            AsyncMock(return_value=tier_resp),
        ):
            client = AsyncMock(spec=httpx.AsyncClient)
            result = await collector._collect_via_api(client)

    assert len(result) >= 1
    assert any("Gemini" in r["service_name"] for r in result)


@pytest.mark.asyncio
async def test_gemini_local_no_dirs_returns_empty():
    """When Gemini session dirs don't exist, local strategy returns empty list."""
    collector = GeminiCollector()

    with patch(
        "app.services.collectors.gemini_local.is_local_collector_enabled", return_value=True
    ):
        with patch("os.path.isdir", return_value=False):
            result = await collector._collect_via_logs(None)

    assert result == []


@pytest.mark.asyncio
async def test_gemini_local_disabled_returns_empty():
    """When local collector is disabled, local strategy returns empty list."""
    collector = GeminiCollector()

    with patch(
        "app.services.collectors.gemini_local.is_local_collector_enabled", return_value=False
    ):
        result = await collector._collect_via_logs(None)

    assert result == []
