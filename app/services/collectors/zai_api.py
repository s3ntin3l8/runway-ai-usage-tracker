"""
zAI API (Balance) collector with prepaid balance tracking.

Collection Strategy:
- Requires ZAI_API_KEY environment variable (Zhipu API key)
- Calls https://open.bigmodel.cn/api/paas/v4/users/me/balance
- Returns prepaid account balance in Chinese Yuan (¥)
- Prepaid model: no quotas, just account balance

See Also:
- zai_plan.py for quota limits (TOKENS_LIMIT, TIME_LIMIT)

Error Handling:
- Missing/invalid keys: Returns error card with key validation message
- API errors (401, etc.): Returns error card with HTTP status
- Connection failures: Returns error card with generic message

Key Validation:
- Checks that key is not literally "zai" (placeholder detection)
"""

from typing import List, Dict, Any
import httpx
from app.core.config import settings
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector


class ZaiApiCollector(BaseCollector):
    """Collector for zAI API (Zhipu AI/GLM) prepaid balance."""

    def _fallback_strategies(self) -> List[Any]:
        """Return the strategy list for zAI API."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect zAI prepaid balance via API."""
        return await self._strategy_api(client)

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return fallback error when API fails."""
        key = settings.ZAI_API_KEY
        if not key or key.lower() == "zai":
            return [
                error_card(
                    "zAI", "🌐", "Missing/Invalid Key", error_type="missing_config"
                )
            ]
        return [error_card("zAI", "🌐", "API Error", error_type="api_error")]

    async def _strategy_api(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect zAI prepaid balance via API."""
        key = settings.ZAI_API_KEY
        if not key or key.lower() == "zai":
            return []

        try:
            resp = await client.get(
                "https://open.bigmodel.cn/api/paas/v4/users/me/balance",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10.0,
            )

            if resp.status_code != 200:
                return []

            data = resp.json()
            bal = float(data.get("data", {}).get("available_balance", 0))

            return [
                {
                    "service": "zAI API",
                    "icon": "🌐",
                    "remaining": f"¥{bal:.2f}",
                    "unit": "balance",
                    "reset": "Manual",
                    "health": "good" if bal > 10 else "warning",
                    "pace": "Stable",
                    "detail": "Prepaid balance (API)",
                }
            ]
        except (httpx.RequestError, ValueError, KeyError, TypeError):
            return []

