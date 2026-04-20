import asyncio
import copy
import logging
import time
from typing import Any

import httpx

from app.core.utils import error_card
from app.services.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


class SmartCollector:
    """
    Wrapper around a BaseCollector that implements differential fetching.

    Tracks:
    - Last successful result and timestamp
    - Consecutive error count
    - Last fetch time and status

    Strategies:
    - Fresh fetch: If cache is stale or errors exceeded
    - Return cached: If fresh data available
    - Return stale: If fetch fails but cache exists
    - Return error: If no cache and fetch fails
    """

    def __init__(
        self,
        collector: BaseCollector,
        collector_name: str,
        ttl: float = 300.0,
        error_threshold: int = 3,
        error_retry_delay: float = 30.0,
    ):
        """
        Initialize SmartCollector wrapper.

        Args:
            collector: The underlying BaseCollector to wrap
            collector_name: Human-readable name (e.g., "Anthropic")
            ttl: Time-to-live in seconds (default 5 minutes)
            error_threshold: Consecutive errors before forcing retry (default 3)
            error_retry_delay: Minimum seconds to wait between retry attempts (default 30s)
        """
        self.collector = collector
        self.collector_name = collector_name
        self.ttl = ttl
        self.error_threshold = error_threshold
        self.error_retry_delay = error_retry_delay

        # Concurrency control
        self._lock = asyncio.Lock()

        # State tracking
        self.last_result: list[dict[str, Any]] | None = None
        self.last_success_time: float | None = None
        self.last_fetch_time: float | None = None
        self.consecutive_errors: int = 0
        self.last_error_message: str | None = None
        self.cache_age_seconds: float = 0.0

    def _should_use_cache(self, now: float) -> bool:
        """
        Determine if cached result is still fresh.

        Returns True if:
        - Cache exists AND
        - TTL not exceeded AND
        - Error count below threshold
        """
        if self.last_result is None:
            return False

        if self.last_success_time is None:
            return False

        last_success = self.last_success_time or 0.0
        age = now - last_success

        # If error threshold exceeded, force fresh fetch attempt
        if self.consecutive_errors >= self.error_threshold:
            logger.debug(
                f"{self.collector_name}: Cache skipped due to error threshold "
                f"({self.consecutive_errors}/{self.error_threshold})"
            )
            return False

        # Cache is stale if age exceeds TTL
        if age > self.ttl:
            logger.debug(
                f"{self.collector_name}: Cache expired (age: {age:.1f}s, ttl: {self.ttl}s)"
            )
            return False

        logger.debug(
            f"{self.collector_name}: Using cached result (age: {age:.1f}s, ttl: {self.ttl}s)"
        )
        return True

    def _should_retry_after_error(self, now: float) -> bool:
        """
        Determine if enough time has passed to retry after an error.

        Returns False if we're still in the error retry delay window.
        This prevents hammering the API during outages.
        """
        if self.last_fetch_time is None or self.consecutive_errors == 0:
            return True

        time_since_last_fetch = now - self.last_fetch_time
        return time_since_last_fetch >= self.error_retry_delay

    def _mark_success(self, result: list[dict[str, Any]], now: float) -> None:
        """Record successful fetch."""
        self.last_result = result
        self.last_success_time = now
        self.last_fetch_time = now
        self.consecutive_errors = 0
        self.last_error_message = None

        # Extract unique sources from results
        sources = sorted(list({str(r.get("data_source", "unknown")) for r in result}))
        source_str = f" [source: {', '.join(sources)}]" if sources else ""

        logger.info(f"{self.collector_name}: Successful fetch ({len(result)} cards){source_str}")

    def _mark_failure(self, error: Exception, now: float) -> None:
        """Record failed fetch."""
        self.consecutive_errors += 1
        self.last_fetch_time = now
        self.last_error_message = str(error)

        logger.warning(
            f"{self.collector_name}: Fetch failed "
            f"(error {self.consecutive_errors}/{self.error_threshold}): {error}"
        )

    async def collect(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """
        Intelligently fetch data with differential fetching strategy.

        Strategy:
        0. If collector is NOT configured, return [] (hides from UI)
        1. If cache is fresh, return it
        2. Acquire lock to prevent concurrent fetch attempts
        3. Double-check cache (it might have been updated while waiting for lock)
        4. If cache is stale or errors exceeded, attempt fresh fetch
        5. Return cached data during failures (graceful degradation)
        """
        # Fast path 1: Skip if not configured (no lock needed)
        if not await self.collector.is_configured():
            return []

        # Fast path 2: Return cached data if fresh (no lock needed for read-only check)
        now = time.time()
        if self._should_use_cache(now) and self.last_result is not None:
            return self._tag_as_cached(self.last_result, now)

        # Acquire lock to ensure only one fetch happens per collector
        async with self._lock:
            # Re-check cache after acquiring lock
            now = time.time()
            if self._should_use_cache(now) and self.last_result is not None:
                return self._tag_as_cached(self.last_result, now)

            # Don't hammer the API during outages
            if not self._should_retry_after_error(now):
                last_fetch = self.last_fetch_time or 0.0
                logger.debug(f"{self.collector_name}: Still in retry delay ({self.error_retry_delay}s)")
                if self.last_result:
                    return self._tag_as_cached(self.last_result, now)
                return [
                    error_card(
                        self.collector_name,
                        "⏳",
                        f"Retry in {self.error_retry_delay - (now - last_fetch):.0f}s",
                        error_type="rate_limited",
                    )
                ]

            # Attempt fresh fetch
            try:
                logger.info(f"{self.collector_name}: Fetching fresh data...")
                result = await self.collector.collect(client)

                if result:
                    self._mark_success(result, now)
                    return copy.deepcopy(result)
                    
                # Empty result without error
                self._mark_failure(Exception("Empty result from collector"), now)
                if self.last_result:
                    return self._tag_as_cached(self.last_result, now)
                return [
                    error_card(
                        self.collector_name,
                        "❌",
                        "No data available",
                        error_type="parse_error",
                    )
                ]

            except Exception as e:
                self._mark_failure(e, now)

                # Graceful degradation: Use stale data if available
                if self.last_result:
                    logger.info(
                        f"{self.collector_name}: Returning cached data due to fetch failure: {e}"
                    )
                    return self._tag_as_cached(self.last_result, now)

                # No cache: Return error card
                return [
                    error_card(
                        self.collector_name,
                        "❌",
                        f"Collection failed: {str(e)[:40]}",
                        error_type="api_error",
                    )
                ]

    def _tag_as_cached(self, result: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """
        Add [Cached X seconds ago] tag to detail field.
        Preserves original data_source and input_source.

        Args:
            result: Original result from collector
            now: Current timestamp

        Returns:
            Result with updated detail field including cache age
        """
        last_success = self.last_success_time or 0.0
        age = now - last_success
        age_str = f"{age:.0f}s" if age < 60 else f"{age / 60:.1f}m"

        tagged = []
        for card in result:
            card_copy = copy.deepcopy(card)
            original_detail = card_copy.get("detail", "")
            card_copy["detail"] = f"{original_detail} [Cached {age_str} ago]"
            # Preserve original data_source and input_source if they exist
            if "data_source" not in card_copy:
                card_copy["data_source"] = "cache"
            tagged.append(card_copy)

        return tagged

    def get_stats(self) -> dict[str, Any]:
        """
        Get internal state statistics for monitoring/debugging.

        Returns:
            Dictionary with cache stats, error counts, etc.
        """
        now = time.time()
        return {
            "collector": self.collector_name,
            "cache_status": {
                "has_cache": self.last_result is not None,
                "cache_age_seconds": (
                    now - self.last_success_time if self.last_success_time else 0
                ),
                "cache_ttl_seconds": self.ttl,
            },
            "error_tracking": {
                "consecutive_errors": self.consecutive_errors,
                "error_threshold": self.error_threshold,
                "last_error": self.last_error_message,
            },
            "timing": {
                "last_fetch_time": self.last_fetch_time,
                "last_success_time": self.last_success_time,
                "error_retry_delay": self.error_retry_delay,
            },
            "locked": self._lock.locked(),
        }

    async def reset(self):
        """Reset the collector and its wrapper state."""
        async with self._lock:
            self.consecutive_errors = 0
            self.last_error_message = None
            self.last_fetch_time = None
            self.last_result = None  # Clear cache to force fresh fetch
            await self.collector.reset()
            logger.info(f"SmartCollector {self.collector_name} reset.")
