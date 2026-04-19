"""
Base collector class for all AI provider quota collectors.

This module defines the abstract interface that all provider-specific collectors
must implement. Each collector follows a 3-tier fallback pattern:
1. Primary Strategy: Direct API calls (OAuth, REST API, etc.)
2. Secondary Strategy: Local log parsing (CLI logs, cache files, etc.)
3. Tertiary Strategy: Error cards or graceful degradation
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """
    Abstract base class for all AI provider quota collectors.
    Now supports multi-account isolation.
    """

    # Subclasses override these to auto-populate Phase 0B fields on every card.
    PROVIDER_ID: str = "unknown"
    DEFAULT_WINDOW_TYPE: str = "unknown"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        """
        Initialize BaseCollector.

        Args:
            account_id: Unique identifier for the account (None for default/ENV)
            account_label: Human-readable account label (e.g. email)
        """
        self.account_id = account_id
        self.account_label = account_label

    async def is_configured(self) -> bool:
        """
        Check if the collector has the necessary credentials/configuration to run.
        Subclasses should override this to check for API keys, session tokens, etc.
        If return False, SmartCollector will skip collection and return empty results
        instead of showing an error card.
        """
        return True

    def _is_valid_credential(self, value: str | None) -> bool:
        """
        Check if a credential value is actually a valid key/token rather than a placeholder.
        Rejects:
        - None or empty strings
        - Values starting with '#' (unparsed .env comments)
        - Values containing common placeholder symbols like '→' or '[UI]'
        """
        if not value:
            return False

        stripped = value.strip()
        if not stripped:
            return False

        # Reject values starting with # (parsed from .env comments)
        if stripped.startswith("#"):
            return False

        # Reject obvious placeholders from .env templates
        placeholders = ["→", "[UI]", "Dashboard", "Settings", "placeholder"]
        if any(p in stripped for p in placeholders):
            return False

        return True

    def _is_error_result(self, results: list[dict[str, Any]]) -> bool:
        """Return True if results are empty or contain an error card."""
        return not results or any(r.get("remaining") == "ERR" for r in results)

    async def collect(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """
        Orchestrate collection strategy with fallbacks and error handling.
        Automatically tags results with account identifiers.
        """
        try:
            # 1. Try Primary Strategy
            results = await self._primary_strategy(client)
            if not self._is_error_result(results):
                return self._tag_results(results)

            # 2. Try Fallbacks
            for strategy in self._fallback_strategies():
                try:
                    results = await strategy(client)
                    if not self._is_error_result(results):
                        return self._tag_results(results)
                except Exception as e:
                    logger.warning(f"Fallback strategy failed: {e}")

            # 3. All failed
            return self._tag_results(await self._error_handler())

        except Exception as e:
            logger.error(f"Collector {self.__class__.__name__} failed: {e}")
            return self._tag_results(
                [
                    {
                        "service_name": "Collector Error",
                        "icon": "⚠️",
                        "remaining": "ERR",
                        "unit": "fail",
                        "reset": "—",
                        "pace": "Stopped",
                        "detail": f"Internal Error: {str(e)[:30]}",
                        "health": "critical",
                    }
                ]
            )

    def _tag_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Add account identifiers to every card in the result list.
        Also attempts to discover account_label if it is missing by scanning card details.
        """
        if not results:
            return []

        # 1. Try to discover account_label if missing or currently "default"
        if not self.account_label or self.account_label.lower() == "default":
            import re

            discovered_label = None
            for card in results:
                detail = card.get("detail", "")
                if not detail:
                    continue
                # Simple email/identity regex for discovery
                # Looks for email-like strings or "org: ..." patterns
                match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", detail)
                if match:
                    discovered_label = match.group(1)
                    break

                # Fallback to org pattern or standalone username after separator (· or |)
                org_match = re.search(r"org:\s*([^\s·\[\]|]+)", detail)
                if org_match:
                    discovered_label = f"org: {org_match.group(1)}"
                    break

                # Standalone username after a dot/separator e.g. "· username" or "| username"
                # This version handles trailing brackets like [Sidecar] or [Statusline]
                user_match = re.search(
                    r"[·|]\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|[a-zA-Z0-9_-]+)(?:\s*\[[^\]]+\])?$",
                    detail,
                )
                if user_match:
                    discovered_label = user_match.group(1)
                    break

            if discovered_label:
                self.account_label = discovered_label

        # 2. Tag cards
        for card in results:
            if "account_id" not in card or not card.get("account_id"):
                card["account_id"] = self.account_id or "default"

            # Use self.account_label if set, otherwise try to use card's existing label, fallback to "Default"
            if (
                "account_label" not in card
                or not card.get("account_label")
                or card.get("account_label").lower() == "default"
            ):
                card["account_label"] = self.account_label or "Default"

            # Phase 0B: inject provider_id and window_type from class constants
            if "provider_id" not in card or not card.get("provider_id"):
                card["provider_id"] = self.PROVIDER_ID
            if "window_type" not in card or card.get("window_type") == "unknown":
                if self.DEFAULT_WINDOW_TYPE != "unknown":
                    card["window_type"] = self.DEFAULT_WINDOW_TYPE

            # Phase 1: Ensure timestamps are timezone-aware ISO strings
            from datetime import UTC, datetime

            now_iso = datetime.now(UTC).isoformat()

            if "updated_at" not in card or not card.get("updated_at"):
                card["updated_at"] = now_iso
            elif isinstance(card.get("updated_at"), str):
                # Ensure existing string has timezone offset
                ua = card["updated_at"]
                if "T" in ua and "+" not in ua and not ua.endswith("Z"):
                    card["updated_at"] = f"{ua}Z"

            if (
                "reset_at" in card
                and card.get("reset_at")
                and isinstance(card.get("reset_at"), str)
            ):
                ra = card["reset_at"]
                if "T" in ra and "+" not in ra and not ra.endswith("Z"):
                    card["reset_at"] = f"{ra}Z"

        return results

    @abstractmethod
    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Execute the primary (usually API) collection strategy."""
        pass

    @abstractmethod
    def _fallback_strategies(
        self,
    ) -> list[Callable[[httpx.AsyncClient], Awaitable[list[dict[str, Any]]]]]:
        """Return an ordered list of fallback async methods to execute."""
        pass

    @abstractmethod
    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return the error card(s) when all strategies fail."""
        pass

    async def reset(self):
        """Reset collector state (e.g., terminal failures). Subclasses should override if needed."""
        pass
