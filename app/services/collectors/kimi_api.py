"""
Kimi API (Balance) collector with prepaid balance tracking.

Collection Strategy:
- Requires KIMI_API_KEY environment variable (Moonshot API key)
- Calls https://api.moonshot.cn/v1/users/me/balance
- Returns prepaid account balance in USD ($)
- Prepaid model: no quotas, just account balance

See Also:
- kimi_coding.py for IDE quota limits (weekly + rate limits)

Error Handling:
- Missing/invalid keys: Returns error card with key validation message
- API errors (401, etc.): Returns error card with HTTP status
- Connection failures: Returns error card with generic message

Key Validation:
- Checks that key length >= 10 (minimum valid key length)
"""

from typing import List, Dict, Any
import httpx
from app.core.config import settings
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector


class KimiApiCollector(BaseCollector):
    """Collector for Kimi API (Moonshot AI) prepaid balance."""

    def _fallback_strategies(self) -> List[Any]:
        """Return the fallback strategies for Kimi API."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect Kimi prepaid balance via API."""
        return await self._strategy_api(client)

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return fallback error when API fails."""
        key = settings.KIMI_API_KEY
        if not key or len(key) < 10:
            return [
                error_card(
                    "Kimi API", "🌙", "Missing/Invalid Key", error_type="missing_config"
                )
            ]
        return [error_card("Kimi API", "🌙", "Unauthorized", error_type="api_error")]

    async def _strategy_api(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect Kimi prepaid balance via API."""
        key = settings.KIMI_API_KEY
        if not key or len(key) < 10:
            return []

        try:
            resp = await client.get(
                "https://api.moonshot.cn/v1/users/me/balance",
                headers={"Authorization": f"Bearer {key}"},
            )

            if resp.status_code != 200:
                # Specialized error cards can be handled via _get_fallback_error if needed, 
                # or returned here if we want them to stop the chain.
                return []

            data = resp.json()
            bal = float(data.get("data", {}).get("available_balance", 0))

            return [
                {
                    "service": "Kimi API",
                    "icon": "🌙",
                    "remaining": f"${bal:.2f}",
                    "unit": "balance",
                    "reset": "Manual",
                    "health": "good" if bal > 5 else "warning",
                    "pace": "Stable",
                    "detail": "Prepaid balance (API)",
                }
            ]
        except (httpx.RequestError, ValueError, KeyError, TypeError):
            return []

