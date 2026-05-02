"""
Unit tests for SmartCollector differential fetching.

Tests cover:
- Cache hit scenarios (fresh data)
- Cache miss scenarios (stale data, no cache)
- Error handling with graceful degradation
- Error threshold tracking and retry logic
- Error retry delay backoff
- Cache age tagging
"""

import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.collectors.base import BaseCollector
from app.services.smart_collector import SmartCollector


@pytest.fixture
def mock_collector():
    """Create a mock collector."""
    collector = AsyncMock(spec=BaseCollector)
    collector.PROVIDER_ID = "test_provider"
    return collector


@pytest.fixture
def mock_client():
    """Create a mock httpx client."""
    return AsyncMock(spec=httpx.AsyncClient)


class TestSmartCollectorCaching:
    """Test caching behavior of SmartCollector."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_data(self, mock_collector, mock_client):
        """Test that fresh cache is returned without fetching."""
        # Setup: Pre-populate cache
        cached_data = [{"service_name": "Test", "remaining": "100%"}]
        mock_collector.collect.return_value = cached_data

        smart = SmartCollector(
            mock_collector,
            "TestCollector",
            ttl=600.0,
            error_threshold=3,
            error_retry_delay=0,
        )

        # First call - populates cache
        result1 = await smart.collect(mock_client)
        assert result1 == cached_data
        assert mock_collector.collect.call_count == 1

        # Second call immediately - should return cache without calling collector
        result2 = await smart.collect(mock_client)
        assert mock_collector.collect.call_count == 1  # Still 1, not 2
        assert "[Cached" in str(result2[0].get("detail", ""))

    @pytest.mark.asyncio
    async def test_cache_expiration_triggers_fresh_fetch(self, mock_collector, mock_client):
        """Test that expired cache triggers fresh fetch."""
        cached_data = [{"service_name": "Test", "remaining": "100%"}]
        fresh_data = [{"service_name": "Test", "remaining": "50%"}]
        mock_collector.collect.side_effect = [cached_data, fresh_data]

        smart = SmartCollector(
            mock_collector,
            "TestCollector",
            ttl=0.1,
            error_threshold=3,
            error_retry_delay=0,
        )

        # First call - populates cache
        result1 = await smart.collect(mock_client)
        assert result1 == cached_data

        # Wait for cache to expire
        await asyncio.sleep(0.15)

        # Second call - cache expired, should fetch fresh
        result2 = await smart.collect(mock_client)
        assert result2 == fresh_data
        assert mock_collector.collect.call_count == 2

    @pytest.mark.asyncio
    async def test_no_cache_always_fetches(self, mock_collector, mock_client):
        """Test that first call always fetches."""
        data = [{"service_name": "Test", "remaining": "100%"}]
        mock_collector.collect.return_value = data

        smart = SmartCollector(mock_collector, "Test", ttl=600.0, error_retry_delay=0)

        result = await smart.collect(mock_client)
        assert result == data
        assert mock_collector.collect.call_count == 1

    @pytest.mark.asyncio
    async def test_unconfigured_collector_returns_empty_list(self, mock_collector, mock_client):
        """Test that unconfigured collector returns empty list without fetching."""
        mock_collector.is_configured.return_value = False
        mock_collector.collect.return_value = [{"service_name": "ShouldNotAppear"}]

        smart = SmartCollector(mock_collector, "Test", ttl=600.0)

        result = await smart.collect(mock_client)
        assert result == []
        assert mock_collector.collect.call_count == 0


class TestSmartCollectorErrorHandling:
    """Test error handling and graceful degradation."""

    @pytest.mark.asyncio
    async def test_error_with_cache_returns_stale_data(self, mock_collector, mock_client):
        """Test that fetch errors return cached data instead of error card."""
        cached_data = [{"service_name": "Test", "remaining": "100%", "detail": "Fresh"}]

        # First call succeeds, second call fails
        mock_collector.collect.side_effect = [cached_data, Exception("API timeout")]

        smart = SmartCollector(
            mock_collector,
            "TestCollector",
            ttl=0.1,
            error_threshold=3,
            error_retry_delay=0,
        )

        # First call - populates cache
        result1 = await smart.collect(mock_client)
        assert result1 == cached_data

        # Wait for cache to expire
        await asyncio.sleep(0.15)

        # Second call - fetch fails, should return cached data
        result2 = await smart.collect(mock_client)
        assert len(result2) == 1
        assert "Test" in result2[0].get("service_name", "")
        assert "[Cached" in result2[0].get("detail", "")  # Tagged as stale

    @pytest.mark.asyncio
    async def test_error_without_cache_returns_error_card(self, mock_collector, mock_client):
        """Test that error without cache returns error card."""
        mock_collector.collect.side_effect = Exception("Connection failed")

        smart = SmartCollector(mock_collector, "TestCollector", error_retry_delay=0)

        result = await smart.collect(mock_client)

        # Should return error card
        assert len(result) == 1
        assert result[0].get("remaining") == "ERR"
        assert "TestCollector" in result[0].get("service_name", "")

    @pytest.mark.asyncio
    async def test_consecutive_errors_tracked(self, mock_collector, mock_client):
        """Test that consecutive errors are counted."""
        mock_collector.collect.side_effect = Exception("API error")

        smart = SmartCollector(
            mock_collector,
            "TestCollector",
            ttl=0.01,
            error_threshold=3,
            error_retry_delay=0,
        )

        # First call - error 1
        result1 = await smart.collect(mock_client)
        assert smart.consecutive_errors == 1

        # Wait for cache to be considered expired
        await asyncio.sleep(0.02)

        # Second call - error 2
        result2 = await smart.collect(mock_client)
        assert smart.consecutive_errors == 2

        # Wait for cache to be considered expired
        await asyncio.sleep(0.02)

        # Third call - error 3
        result3 = await smart.collect(mock_client)
        assert smart.consecutive_errors == 3

    @pytest.mark.asyncio
    async def test_success_resets_error_count(self, mock_collector, mock_client):
        """Test that successful fetch resets error counter."""
        data = [{"service_name": "Test", "remaining": "100%"}]

        # Fail twice, then succeed
        mock_collector.collect.side_effect = [
            Exception("Error 1"),
            Exception("Error 2"),
            data,
        ]

        smart = SmartCollector(
            mock_collector,
            "TestCollector",
            ttl=0.01,
            error_threshold=3,
            error_retry_delay=0,
        )

        # First two calls fail
        await smart.collect(mock_client)
        assert smart.consecutive_errors == 1

        await asyncio.sleep(0.02)
        await smart.collect(mock_client)
        assert smart.consecutive_errors == 2

        # Third call succeeds
        await asyncio.sleep(0.02)
        result = await smart.collect(mock_client)
        assert result == data
        assert smart.consecutive_errors == 0


class TestSmartCollectorErrorThreshold:
    """Test error threshold behavior."""

    @pytest.mark.asyncio
    async def test_error_threshold_triggers_retry(self, mock_collector, mock_client):
        """Test that error threshold forces fetch attempt."""
        # First call: error, cache set to None
        # Subsequent calls: should still try to fetch even though cache is stale
        mock_collector.collect.side_effect = [
            Exception("Error"),
            [{"service_name": "Recovered", "remaining": "100%"}],
        ]

        smart = SmartCollector(
            mock_collector,
            "TestCollector",
            ttl=600.0,
            error_threshold=1,
            error_retry_delay=0,
        )

        # First call fails
        result1 = await smart.collect(mock_client)
        assert smart.consecutive_errors == 1

        # Since we hit error threshold (1), next call should attempt fresh fetch
        # despite having long TTL (because error_threshold was exceeded)
        result2 = await smart.collect(mock_client)
        assert "Recovered" in result2[0].get("service_name", "")
        assert mock_collector.collect.call_count == 2


class TestSmartCollectorRetryDelay:
    """Test error retry delay backoff."""

    @pytest.mark.asyncio
    async def test_retry_delay_prevents_hammering(self, mock_collector, mock_client):
        """Test that retry delay prevents rapid retries."""
        mock_collector.collect.side_effect = Exception("API down")

        smart = SmartCollector(mock_collector, "TestCollector", error_retry_delay=0.2)

        # First failure
        result1 = await smart.collect(mock_client)
        assert mock_collector.collect.call_count == 1

        # Immediate retry - should skip fetch due to retry delay
        result2 = await smart.collect(mock_client)
        assert mock_collector.collect.call_count == 1  # Still 1, not fetched

        # Wait for retry delay
        await asyncio.sleep(0.25)

        # Now should attempt fetch
        result3 = await smart.collect(mock_client)
        assert mock_collector.collect.call_count == 2


class TestSmartCollectorCacheTags:
    """Test cache age tagging functionality."""

    @pytest.mark.asyncio
    async def test_cache_tagged_with_age(self, mock_collector, mock_client):
        """Test that returned cached data is tagged with age."""
        data = [{"service_name": "Test", "remaining": "100%", "detail": "Original detail"}]
        mock_collector.collect.return_value = data

        smart = SmartCollector(mock_collector, "TestCollector", ttl=600.0, error_retry_delay=0)

        # First call - populate cache
        result1 = await smart.collect(mock_client)
        original_detail = result1[0].get("detail")

        # Return cached (no fetch)
        result2 = await smart.collect(mock_client)
        cached_detail = result2[0].get("detail")

        # Should have tag
        assert "[Cached" in cached_detail
        assert "ago]" in cached_detail
        # Should preserve original detail
        assert original_detail in cached_detail


class TestSmartCollectorRateLimit:
    """Test 429 rate-limit handling."""

    @pytest.mark.asyncio
    async def test_429_backoff_set_from_error_card(self, mock_collector, mock_client):
        """Test that SmartCollector sets 429 backoff when collector returns rate_limited card."""
        rate_limited = [
            {
                "service_name": "Test",
                "remaining": "ERR",
                "error_type": "rate_limited",
                "detail": "Rate limited",
            }
        ]
        mock_collector.collect.return_value = rate_limited
        mock_collector._last_retry_after = 60.0

        smart = SmartCollector(mock_collector, "TestCollector", error_retry_delay=0)

        result = await smart.collect(mock_client)
        assert result[0].get("error_type") == "rate_limited"
        assert smart._is_429_backoff_active(smart.last_fetch_time or 0)
        assert smart._last_retry_after == 60.0

    @pytest.mark.asyncio
    async def test_429_backoff_prevents_fetch(self, mock_collector, mock_client):
        """Test that 429 backoff prevents collector from being called again."""
        rate_limited = [
            {
                "service_name": "Test",
                "remaining": "ERR",
                "error_type": "rate_limited",
                "detail": "Rate limited",
            }
        ]
        good_data = [{"service_name": "Test", "remaining": "100%"}]

        mock_collector.collect.side_effect = [rate_limited, good_data]
        mock_collector._last_retry_after = 0.2

        smart = SmartCollector(mock_collector, "TestCollector", error_retry_delay=0)

        # First call triggers 429
        await smart.collect(mock_client)
        assert smart._is_429_backoff_active(smart.last_fetch_time or 0)
        assert mock_collector.collect.call_count == 1

        # Second call during backoff should skip fetch
        result2 = await smart.collect(mock_client)
        assert mock_collector.collect.call_count == 1  # Still 1
        assert result2[0].get("error_type") == "rate_limited"

    @pytest.mark.asyncio
    async def test_429_backoff_expires_and_allows_fetch(self, mock_collector, mock_client):
        """Test that 429 backoff expires and allows fetching again."""
        rate_limited = [
            {
                "service_name": "Test",
                "remaining": "ERR",
                "error_type": "rate_limited",
                "detail": "Rate limited",
            }
        ]
        good_data = [{"service_name": "Test", "remaining": "100%"}]

        mock_collector.collect.side_effect = [rate_limited, good_data]
        mock_collector._last_retry_after = 0.15

        smart = SmartCollector(mock_collector, "TestCollector", error_retry_delay=0)

        # First call triggers 429
        await smart.collect(mock_client)
        assert smart._is_429_backoff_active(smart.last_fetch_time or 0)

        # Wait for backoff to expire
        await asyncio.sleep(0.2)

        # Should now fetch fresh data
        result2 = await smart.collect(mock_client)
        assert result2[0].get("remaining") == "100%"
        assert mock_collector.collect.call_count == 2
        assert not smart._is_429_backoff_active(time.time())

    @pytest.mark.asyncio
    async def test_429_default_retry_after_when_not_set(self, mock_collector, mock_client):
        """Test default 60s retry-after when collector doesn't set _last_retry_after."""
        rate_limited = [
            {
                "service_name": "Test",
                "remaining": "ERR",
                "error_type": "rate_limited",
                "detail": "Rate limited",
            }
        ]
        mock_collector.collect.return_value = rate_limited
        # No _last_retry_after set

        smart = SmartCollector(mock_collector, "TestCollector", error_retry_delay=0)

        await smart.collect(mock_client)
        assert smart._last_retry_after == 60.0  # Default

    @pytest.mark.asyncio
    async def test_success_clears_429_backoff(self, mock_collector, mock_client):
        """Test that successful fetch clears 429 backoff state."""
        rate_limited = [
            {
                "service_name": "Test",
                "remaining": "ERR",
                "error_type": "rate_limited",
                "detail": "Rate limited",
            }
        ]
        good_data = [{"service_name": "Test", "remaining": "100%"}]

        mock_collector.collect.side_effect = [rate_limited, good_data]
        mock_collector._last_retry_after = 0.15

        smart = SmartCollector(mock_collector, "TestCollector", ttl=0.01, error_retry_delay=0)

        # First call: 429
        await smart.collect(mock_client)
        assert smart._is_429_backoff_active(smart.last_fetch_time or 0)

        # Wait for cache and 429 backoff to expire
        await asyncio.sleep(0.2)

        # Second call: success should clear backoff
        await smart.collect(mock_client)
        assert not smart._is_429_backoff_active(time.time())
        assert smart._last_429_time is None

    @pytest.mark.asyncio
    async def test_429_stats_exposed(self, mock_collector, mock_client):
        """Test that get_stats exposes 429 backoff state."""
        rate_limited = [
            {
                "service_name": "Test",
                "remaining": "ERR",
                "error_type": "rate_limited",
                "detail": "Rate limited",
            }
        ]
        mock_collector.collect.return_value = rate_limited
        mock_collector._last_retry_after = 120.0

        smart = SmartCollector(mock_collector, "TestCollector", error_retry_delay=0)
        await smart.collect(mock_client)

        stats = smart.get_stats()
        assert stats["rate_limit"]["is_backoff_active"] is True
        assert stats["rate_limit"]["backoff_seconds_remaining"] > 0
        assert stats["rate_limit"]["retry_after_header"] == 120.0


class TestSmartCollectorStats:
    """Test statistics tracking."""

    @pytest.mark.asyncio
    async def test_get_stats_returns_collector_state(self, mock_collector, mock_client):
        """Test that get_stats returns current state."""
        data = [{"service_name": "Test", "remaining": "100%"}]
        mock_collector.collect.return_value = data

        smart = SmartCollector(
            mock_collector,
            "TestCollector",
            ttl=600.0,
            error_threshold=3,
            error_retry_delay=0,
        )

        # Populate cache
        await smart.collect(mock_client)

        stats = smart.get_stats()

        # Verify stats structure
        assert stats["collector"] == "TestCollector"
        assert stats["cache_status"]["has_cache"] is True
        assert stats["cache_status"]["cache_ttl_seconds"] == 600.0
        assert stats["error_tracking"]["consecutive_errors"] == 0
        assert stats["error_tracking"]["error_threshold"] == 3


class TestSmartCollectorIntegration:
    """Integration tests with realistic scenarios."""

    @pytest.mark.asyncio
    async def test_realistic_scenario_with_provider_outage(self, mock_collector, mock_client):
        """Test realistic scenario: provider outage and recovery."""
        good_data = [{"service_name": "Provider", "remaining": "100%"}]

        # Simulate: good -> outage (3 errors) -> recovery
        mock_collector.collect.side_effect = [
            good_data,  # First fetch - success (r1)
            Exception("Timeout"),  # Error 1 (r2)
            Exception("500 Error"),  # Error 2 (r3)
            Exception("Rate limit"),  # Error 3 (r4)
            good_data,  # Recovery (r5)
        ]

        smart = SmartCollector(
            mock_collector,
            "Provider",
            ttl=0.05,
            error_threshold=3,
            error_retry_delay=0.01,
        )

        # First: Success
        r1 = await smart.collect(mock_client)
        assert "Provider" in r1[0].get("service_name", "")

        # Outage begins
        await asyncio.sleep(0.06)  # Cache expires
        r2 = await smart.collect(mock_client)  # Returns cached (error 1)

        # Continue attempting
        await asyncio.sleep(0.02)
        r3 = await smart.collect(mock_client)  # Returns cached (error 2)

        # Error threshold reached, still returns cached
        await asyncio.sleep(0.02)
        r4 = await smart.collect(mock_client)  # Returns cached
        assert smart.consecutive_errors >= 3

        # Recovery: Provider comes back online
        await asyncio.sleep(0.02)
        r5 = await smart.collect(mock_client)
        assert "Provider" in r5[0].get("service_name", "")
        assert smart.consecutive_errors == 0
