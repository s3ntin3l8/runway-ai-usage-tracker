"""
Base collector class for all AI provider quota collectors.

This module defines the abstract interface that all provider-specific collectors
must implement. Each collector follows a 3-tier fallback pattern:
1. Primary Strategy: Direct API calls (OAuth, REST API, etc.)
2. Secondary Strategy: Local log parsing (CLI logs, cache files, etc.)
3. Tertiary Strategy: Error cards or graceful degradation

The collector pattern ensures resilience in headless environments (Docker, CI/CD)
where desktop UI features may not be available.
"""

import httpx
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """
    Abstract base class for all AI provider quota collectors.

    Defines the interface that all provider-specific collectors must implement.
    Collectors are responsible for:
    - Fetching quota and usage data from their respective providers
    - Implementing resilient fallback strategies when APIs are unavailable
    - Returning standardized LimitCard dictionaries for frontend rendering

    The collect() method should be idempotent and handle errors gracefully,
    returning error cards instead of raising exceptions.
    """

    def _is_error_result(self, results: List[Dict[str, Any]]) -> bool:
        """Return True if results are empty or contain an error card."""
        return not results or any(r.get("remaining") == "ERR" for r in results)

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Automated Strategy Pattern orchestration. Executes the primary strategy,
        then fallback strategies sequentially until one succeeds or all fail.
        """
        # 1. Try Primary Strategy
        try:
            results = await self._primary_strategy(client)
            if not self._is_error_result(results):
                return results
            logger.debug(f"Primary strategy returned error/empty, proceeding to fallbacks...")
        except Exception as e:
            logger.warning(f"Primary strategy raised exception: {e}")

        # 2. Try Fallback Strategies
        for strategy in self._fallback_strategies():
            try:
                results = await strategy(client)
                if not self._is_error_result(results):
                    return results

                strategy_name = (
                    strategy.__name__ if hasattr(strategy, "__name__") else "unknown"
                )
                logger.debug(
                    f"Fallback strategy {strategy_name} returned error/empty, falling back..."
                )
            except Exception as e:
                strategy_name = (
                    strategy.__name__ if hasattr(strategy, "__name__") else "unknown"
                )
                logger.warning(f"Fallback strategy {strategy_name} raised exception: {e}")

        # 3. All strategies failed - return final error
        return await self._error_handler()

    @abstractmethod
    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Execute the primary (usually API) collection strategy."""
        pass

    @abstractmethod
    def _fallback_strategies(
        self,
    ) -> List[Callable[[httpx.AsyncClient], Awaitable[List[Dict[str, Any]]]]]:
        """Return an ordered list of fallback async methods to execute if the primary fails."""
        pass

    @abstractmethod
    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return the ultimate error card(s) to display when all strategies fail."""
        pass

