"""
SmartCollector wrapper implementing differential fetching strategy.

This module wraps individual collectors to implement intelligent caching:
- Tracks last successful result and timestamp
- Monitors error patterns (consecutive errors)
- Only fetches fresh data when:
  1. Cache is stale (TTL exceeded)
  2. Previous fetch failed
  3. Error threshold exceeded (forces refresh attempt)
- Returns cached data during failures instead of error cards
- Gradually increases retry frequency when errors accumulate

Benefits:
- Reduced API calls (only fetch when needed)
- Graceful degradation (show stale data vs error cards)
- Automatic recovery attempts
- Per-collector configurable TTL and error thresholds
"""

import time
import logging
import copy
from typing import List, Dict, Any, Optional
import httpx

from app.services.collectors.base import BaseCollector
from app.core.utils import error_card

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
        error_retry_delay: float = 30.0
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
        
        # State tracking
        self.last_result: Optional[List[Dict[str, Any]]] = None
        self.last_success_time: Optional[float] = None
        self.last_fetch_time: Optional[float] = None
        self.consecutive_errors: int = 0
        self.last_error_message: Optional[str] = None
        self.cache_age_seconds: float = 0.0
    
    def _should_use_cache(self) -> bool:
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
        
        age = time.time() - self.last_success_time
        
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
                f"{self.collector_name}: Cache expired "
                f"(age: {age:.1f}s, ttl: {self.ttl}s)"
            )
            return False
        
        logger.debug(
            f"{self.collector_name}: Using cached result "
            f"(age: {age:.1f}s, ttl: {self.ttl}s)"
        )
        return True
    
    def _should_retry_after_error(self) -> bool:
        """
        Determine if enough time has passed to retry after an error.
        
        Returns False if we're still in the error retry delay window.
        This prevents hammering the API during outages.
        """
        if self.last_fetch_time is None or self.consecutive_errors == 0:
            return True
        
        time_since_last_fetch = time.time() - self.last_fetch_time
        return time_since_last_fetch >= self.error_retry_delay
    
    def _mark_success(self, result: List[Dict[str, Any]]) -> None:
        """Record successful fetch."""
        self.last_result = result
        self.last_success_time = time.time()
        self.last_fetch_time = time.time()
        self.consecutive_errors = 0
        self.last_error_message = None
        
        logger.info(
            f"{self.collector_name}: Successful fetch "
            f"({len(result)} cards)"
        )
    
    def _mark_failure(self, error: Exception) -> None:
        """Record failed fetch."""
        self.consecutive_errors += 1
        self.last_fetch_time = time.time()
        self.last_error_message = str(error)
        
        logger.warning(
            f"{self.collector_name}: Fetch failed "
            f"(error {self.consecutive_errors}/{self.error_threshold}): {error}"
        )
    
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Intelligently fetch data with differential fetching strategy.
        
        Strategy:
        1. If cache is fresh, return it
        2. If cache is stale or errors exceeded, attempt fresh fetch
        3. On success: Update cache and reset error count
        4. On failure:
           - If cache exists: Return stale data with [Cached] tag
           - If no cache: Return error card
        5. If in error retry delay: Return cached/error without fetching
        
        Args:
            client: httpx.AsyncClient for making requests
            
        Returns:
            List[Dict[str, Any]]: Fresh data, cached data, or error card
        """
        # Fast path: Return cached data if fresh
        if self._should_use_cache():
            return self._tag_as_cached(self.last_result)
        
        # Don't hammer the API during outages
        if not self._should_retry_after_error():
            logger.debug(
                f"{self.collector_name}: Still in retry delay "
                f"({self.error_retry_delay}s)"
            )
            if self.last_result:
                return self._tag_as_cached(self.last_result)
            return [error_card(
                self.collector_name,
                "⏳",
                f"Retry in {self.error_retry_delay - (time.time() - self.last_fetch_time):.0f}s",
                error_type="rate_limited"
            )]
        
        # Attempt fresh fetch
        try:
            logger.info(f"{self.collector_name}: Fetching fresh data...")
            result = await self.collector.collect(client)
            
            if result:
                self._mark_success(result)
                return copy.deepcopy(result)
            else:
                # Empty result without error
                self._mark_failure(Exception("Empty result from collector"))
                if self.last_result:
                    return self._tag_as_cached(self.last_result)
                return [error_card(
                    self.collector_name,
                    "❌",
                    "No data available",
                    error_type="parse_error"
                )]
        
        except Exception as e:
            self._mark_failure(e)
            
            # Graceful degradation: Use stale data if available
            if self.last_result:
                logger.info(
                    f"{self.collector_name}: Returning cached data "
                    f"due to fetch failure: {e}"
                )
                return self._tag_as_cached(self.last_result)
            
            # No cache: Return error card
            return [error_card(
                self.collector_name,
                "❌",
                f"Collection failed: {str(e)[:40]}",
                error_type="api_error"
            )]
    
    def _tag_as_cached(self, result: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Add [Cached X seconds ago] tag to detail field.
        
        Args:
            result: Original result from collector
            
        Returns:
            Result with updated detail field including cache age
        """
        age = time.time() - self.last_success_time
        age_str = f"{age:.0f}s" if age < 60 else f"{age/60:.1f}m"
        
        tagged = []
        for card in result:
            card_copy = copy.deepcopy(card)
            original_detail = card_copy.get("detail", "")
            card_copy["detail"] = f"{original_detail} [Cached {age_str} ago]"
            tagged.append(card_copy)
        
        return tagged
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get internal state statistics for monitoring/debugging.
        
        Returns:
            Dictionary with cache stats, error counts, etc.
        """
        return {
            "collector": self.collector_name,
            "cache_status": {
                "has_cache": self.last_result is not None,
                "cache_age_seconds": time.time() - self.last_success_time if self.last_success_time else 0,
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
            }
        }
