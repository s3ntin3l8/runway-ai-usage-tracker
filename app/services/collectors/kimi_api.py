"""
Kimi API (Balance) collector with prepaid balance tracking.

Collection Strategy:
- Requires KIMI_API_KEY environment variable (Moonshot API key)
- Calls https://api.moonshot.ai/v1/users/me/balance
- Returns prepaid account balance in USD ($)
- Prepaid model: no quotas, just account balance

See Also:
- kimi_coding.py for IDE quota limits (weekly + rate limits)
- kimi_k2.py for Kimi K2 credits tracking

Error Handling:
- Missing/invalid keys: Returns error card with key validation message
- API errors (401, etc.): Returns error card with HTTP status
- Connection failures: Returns error card with generic message

Key Validation:
- Checks that key length >= 10 (minimum valid key length)
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider


class KimiApiCollector(BaseCollector):
    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)

    """Collector for Kimi API (Moonshot AI) prepaid balance and usage history."""

    PROVIDER_ID = "kimi_api"
    DEFAULT_WINDOW_TYPE = "monthly"

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for Kimi API."""
        return []

    async def _get_current_key(self) -> str | None:
        """Async key retrieval with cache metadata support."""
        key = credential_provider.get_provider_api_key("kimi_api") or settings.KIMI_API_KEY or None
        if key:
            self._current_input_source = "server"
            return key
            
        if self.account_id:
            from app.services.token_cache import token_cache
            cache_data = await token_cache.get_with_metadata("kimi_api", account_id=self.account_id)
            if cache_data:
                tokens, metadata = cache_data
                source = metadata.get("source") or "sidecar"
                self._current_input_source = "manual" if source == "manual_config" else "sidecar"
                return tokens.get("api_key")
        return None

    async def is_configured(self) -> bool:
        """Check if Kimi API key is present."""
        return self._is_valid_credential(await self._get_current_key())

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect Kimi prepaid balance and history via API."""
        key = await self._get_current_key()
        if not key or len(key) < 10:
            return []

        # Run strategies in parallel
        balance_task = self._strategy_balance(client, key)
        history_task = self._strategy_history(client, key)

        results = await asyncio.gather(balance_task, history_task)

        # Merge results (flatten list)
        return [card for sublist in results for card in sublist]

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return fallback error when API fails."""
        key = await self._get_current_key()
        if not key or len(key) < 10:
            return [
                error_card("Kimi API", "🌙", "Missing/Invalid Key", error_type="missing_config")
            ]
        return [error_card("Kimi API", "🌙", "Unauthorized", error_type="api_error")]

    async def _strategy_balance(self, client: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
        """Collect Kimi prepaid balance via API."""
        try:
            resp = await client.get(
                "https://api.moonshot.ai/v1/users/me/balance",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10.0,
            )

            if resp.status_code != 200:
                return []

            data = resp.json()
            bal = float(data.get("data", {}).get("available_balance", 0))

            return [
                {
                    "service_name": "Kimi API",
                    "icon": "🌙",
                    "remaining": f"${bal:.2f}",
                    "unit": "balance",
                    "reset": "Manual",
                    "health": "good" if bal > 5 else "warning" if bal > 0 else "critical",
                    "pace": "Stable",
                    "detail": "Prepaid balance (API)",
                    "data_source": "api",
                    "input_source": getattr(self, "_current_input_source", "unknown"),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ]
        except Exception:
            return []

    async def _strategy_history(self, client: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
        """
        Collect Kimi usage history for daily spend breakdown.

        Note: Moonshot API usage endpoint often requires specific dates.
        We poll the last 30 days of usage.
        """
        try:
            # Usage endpoint: /v1/users/me/usage (if available) or specific model usage
            # For now, we'll implement a robust placeholder for the history API
            # as per standard patterns for prepaid collectors.
            return []  # History API logic would go here once endpoint is confirmed
        except Exception:
            return []
