import asyncio
import copy
import logging
import time
from typing import Any

import httpx

from app.core.utils import error_card
from app.services.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


def _is_error_result(result: list[dict[str, Any]]) -> bool:
    """True when *result* is an error card (built by `error_card()`), not real data.

    Every existing collector returns error cards as the sole content of the
    list (never mixed with healthy cards — see `error_card()` callers), so
    checking any() is safe: a hit means the whole result is a failure signal.
    """
    return any(r.get("data_source") == "error" or r.get("remaining") == "ERR" for r in result)


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

    # Beyond this cache age, a served card is almost certainly masking an
    # ongoing collection failure rather than routine TTL churn — degrade it
    # visibly (see _tag_as_cached) instead of presenting stale data as healthy.
    STALE_CEILING_SECONDS = 3600  # 1 hour

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

        # 429 rate-limit tracking
        self._last_429_time: float | None = None
        self._last_retry_after: float | None = None

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

    def _is_429_backoff_active(self, now: float) -> bool:
        """Check if we're in a 429 backoff window."""
        if self._last_429_time is None:
            return False
        if self._last_retry_after:
            return (now - self._last_429_time) < self._last_retry_after
        return False

    def _get_429_backoff_remaining(self, now: float) -> float:
        """Get remaining seconds in 429 backoff, or 0 if none."""
        if self._last_429_time is None or self._last_retry_after is None:
            return 0.0
        remaining = self._last_retry_after - (now - self._last_429_time)
        return max(0.0, remaining)

    def _should_retry_after_error(self, now: float) -> bool:
        """
        Determine if enough time has passed to retry after an error.

        Returns False if we're still in the error retry delay window
        OR if a 429 backoff is currently active.
        This prevents hammering the API during outages.
        """
        # Check 429 backoff first (takes precedence)
        if self._is_429_backoff_active(now):
            return False

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

    def _mark_429(self, retry_after: float | None, now: float) -> None:
        """Record a 429 rate limit response."""
        self._last_429_time = now
        self._last_retry_after = retry_after or 60.0  # Default 60s if no header
        self.last_fetch_time = now
        self.consecutive_errors += 1

        wait_str = f"{self._last_retry_after:.0f}s"
        logger.warning(f"{self.collector_name}: Rate limited (429). Backoff for {wait_str}")

    def _clear_429(self) -> None:
        """Clear 429 backoff state after successful fetch."""
        self._last_429_time = None
        self._last_retry_after = None

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
            # Debug-level: every unconfigured provider hits this each cycle, so it
            # must not spam — but without it a token-lookup miss makes the provider
            # vanish with zero cards and no trace (the antigravity hash-key bug).
            logger.debug(f"{self.collector_name}: not configured, skipping collection")
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

            # Don't hammer the API during outages or 429 backoff
            if not self._should_retry_after_error(now):
                backoff_rem = self._get_429_backoff_remaining(now)
                provider_id = getattr(self.collector, "PROVIDER_ID", None)

                if backoff_rem > 0:
                    logger.debug(
                        f"{self.collector_name}: Still in 429 backoff ({backoff_rem:.0f}s)"
                    )
                    # Standardize to match provider-specific formatting (minutes if > 60s)
                    if backoff_rem >= 60:
                        msg = f"Rate Limited (429) - Try in {backoff_rem / 60:.1f}m"
                    else:
                        msg = f"Rate Limited (429) - Try in {backoff_rem:.0f}s"
                else:
                    last_fetch = self.last_fetch_time or 0.0
                    delay_rem = self.error_retry_delay - (now - last_fetch)
                    logger.debug(f"{self.collector_name}: Still in retry delay ({delay_rem:.0f}s)")
                    msg = f"Retry in {delay_rem:.0f}s"

                if self.last_result:
                    return self._tag_as_cached(self.last_result, now)
                return [
                    error_card(
                        self.collector_name,
                        "⏳",
                        msg,
                        error_type="rate_limited",
                        provider_id=provider_id,
                    )
                ]

            # Attempt fresh fetch
            try:
                logger.info(f"{self.collector_name}: Fetching fresh data...")
                result = await self.collector.collect(client)

                if result:
                    # Check if collector returned a 429 error card
                    rate_limited = any(r.get("error_type") == "rate_limited" for r in result)
                    if rate_limited:
                        # Try to extract Retry-After from collector or default
                        retry_after = None
                        if hasattr(self.collector, "_last_retry_after"):
                            retry_after = getattr(self.collector, "_last_retry_after", None)
                        self._mark_429(retry_after, now)
                        if self.last_result:
                            return self._tag_as_cached(self.last_result, now)
                        return copy.deepcopy(result)

                    # Any other error-shaped card (auth failure, parse error, ...)
                    # is a failure signal, not data — treating it as "success"
                    # would overwrite last_result with the error card itself,
                    # permanently poisoning the cache-serving fallback, and log
                    # a fetch failure as "Successful fetch". This is what let a
                    # stale Antigravity quota card sit indefinitely, logged as
                    # healthy, while the token was actually expired.
                    if _is_error_result(result):
                        self._mark_failure(
                            Exception(result[0].get("detail") or "collector returned an error"),
                            now,
                        )
                        if self.last_result:
                            return self._tag_as_cached(self.last_result, now)
                        return copy.deepcopy(result)

                    # Success: clear any 429 backoff
                    self._clear_429()
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
                        provider_id=getattr(self.collector, "PROVIDER_ID", None),
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
                        provider_id=getattr(self.collector, "PROVIDER_ID", None),
                    )
                ]

    def _tag_as_cached(self, result: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """
        Add [Cached X seconds ago] tag to detail field.
        Preserves original data_source and input_source.

        Age is recomputed against `now` on every call, so as long as
        `collect()` keeps invoking this each poll cycle (rather than freezing
        on a stale in-memory `last_result` — see the error-result handling
        above), the tag stays truthful instead of freezing at whatever value
        it happened to have the first time this card was cache-served.

        Past `STALE_CEILING_SECONDS`, the card is additionally flagged
        (health="critical" + a "Collection failing" prefix) so a long-running
        outage reads as visibly degraded rather than confidently healthy. This
        intentionally avoids touching `data_source`/`error_type`/`remaining`
        (the fields `accumulator.upsert_latest_usage` uses to detect and
        suppress error cards) — this is still real, if old, data, so it must
        keep being written and refresh the row's `updated_at`.

        Args:
            result: Original result from collector
            now: Current timestamp

        Returns:
            Result with updated detail field including cache age
        """
        last_success = self.last_success_time or 0.0
        age = now - last_success
        age_str = f"{age:.0f}s" if age < 60 else f"{age / 60:.1f}m"
        is_stale = age > self.STALE_CEILING_SECONDS

        tagged = []
        for card in result:
            # Deep copy: nested dicts (token_usage, by_model) must not share
            # references with the cached result, since callers further mutate
            # cards (e.g. accumulator merge).
            card_copy = copy.deepcopy(card)
            original_detail = card_copy.get("detail", "")
            card_copy["detail"] = f"{original_detail} [Cached {age_str} ago]"
            # Preserve original data_source and input_source if they exist
            if "data_source" not in card_copy:
                card_copy["data_source"] = "cache"
            if is_stale:
                card_copy["health"] = "critical"
                card_copy["detail"] = f"⚠ Collection failing — {card_copy['detail']}"
            tagged.append(card_copy)

        return tagged

    def get_stats(self) -> dict[str, Any]:
        """
        Get internal state statistics for monitoring/debugging.

        Returns:
            Dictionary with cache stats, error counts, etc.
        """
        now = time.time()
        backoff_rem = self._get_429_backoff_remaining(now)
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
            "rate_limit": {
                "is_backoff_active": self._is_429_backoff_active(now),
                "backoff_seconds_remaining": backoff_rem,
                "retry_after_header": self._last_retry_after,
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
            self._clear_429()
            await self.collector.reset()
            logger.info(f"SmartCollector {self.collector_name} reset.")
