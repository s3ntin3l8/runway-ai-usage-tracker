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
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from app.models.schemas import LimitCard


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

    @abstractmethod
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect usage limits from the provider and return standardized result cards.

        Implements the 3-tier fallback pattern:
        1. Primary: Direct API/OAuth calls with retry logic
        2. Secondary: Local log/file parsing as fallback
        3. Tertiary: Return error cards describing what failed

        Args:
            client: httpx.AsyncClient instance for making API requests.
                   Reused across collectors to manage connection pooling.

        Returns:
            List[Dict[str, Any]]: List of result dictionaries, each containing:
                - service: str - Provider name (e.g., "Claude Pro", "Gemini API")
                - icon: str - Unicode emoji for visual identification
                - remaining: str - Remaining quota/usage (number or percentage)
                - unit: str - Unit description (e.g., "tokens", "requests")
                - reset: str - Human-readable reset time (e.g., "in 4h 23m")
                - health: str - Status (good/warning/critical/unknown)
                - pace: str - Estimated consumption rate or longevity
                - detail: str - Additional context (data source, error reason, etc.)

        Note:
            Should never raise exceptions. Return error_card() for all failure scenarios.
        """
        pass
