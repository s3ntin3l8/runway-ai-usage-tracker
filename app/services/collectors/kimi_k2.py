"""
Kimi K2 (Credits) collector with credit balance tracking.

Collection Strategy:
- Requires KIMI_K2_API_KEY environment variable (Kimi K2 API key)
- Calls https://kimi-k2.ai/api/user/credits
- Returns credit balance (prepaid credits for coding agent usage)
- Different from kimi_api.py which tracks general Moonshot prepaid balance

See Also:
- kimi_api.py for general Moonshot API balance
- kimi_coding.py for IDE quota limits (weekly + rate limits)

Error Handling:
- Missing/invalid keys: Returns error card with key validation message
- API errors (401, etc.): Returns error card with HTTP status
- Connection failures: Returns error card with generic message

Key Validation:
- Checks that key length >= 10 (minimum valid key length)
"""

from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider


class KimiK2Collector(BaseCollector):
    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)

    """Collector for Kimi K2 credits (coding agent product)."""

    PROVIDER_ID = "kimi_k2"
    DEFAULT_WINDOW_TYPE = "monthly"

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for Kimi K2."""
        return []

    def _get_api_key(self) -> str | None:
        """DB (UI-set) → env var."""
        return (
            credential_provider.get_provider_api_key("kimi_k2") or settings.KIMI_K2_API_KEY or None
        )

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect Kimi K2 credits via API."""
        key = self._get_api_key()
        if not key or len(key) < 10:
            return []

        return await self._strategy_credits(client, key)

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return fallback error when API fails."""
        key = self._get_api_key()
        if not key or len(key) < 10:
            return [error_card("Kimi K2", "🌙", "Missing/Invalid Key", error_type="missing_config")]
        return [error_card("Kimi K2", "🌙", "Unauthorized", error_type="api_error")]

    async def _strategy_credits(self, client: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
        """Collect Kimi K2 credits via API."""
        try:
            resp = await client.get(
                "https://kimi-k2.ai/api/user/credits",
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                timeout=10.0,
            )

            if resp.status_code != 200:
                return []

            data = resp.json()
            credits_remaining = float(data.get("credits_remaining", 0))
            credits_consumed = float(data.get("credits_consumed", 0))

            return [
                {
                    "service_name": "Kimi K2",
                    "icon": "🌙",
                    "remaining": f"{credits_remaining:.2f}",
                    "unit": "credits",
                    "reset": "Manual",
                    "health": "good"
                    if credits_remaining > 100
                    else "warning"
                    if credits_remaining > 0
                    else "critical",
                    "pace": "Stable",
                    "detail": f"Credits (consumed: {credits_consumed:.2f})",
                    "data_source": "api_credits",
                    "is_unlimited": False,
                    "unit_type": "credits",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ]
        except Exception:
            return []
