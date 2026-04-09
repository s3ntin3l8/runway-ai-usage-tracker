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

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Kimi Code (Moonshot AI) prepaid balance.

        Requires KIMI_API_KEY (Moonshot API key with length >= 10).
        Handles 401 Unauthorized separately to distinguish auth issues.
        Returns error card if key missing or API fails.

        Returns:
            List[Dict[str, Any]]: Single card with balance in $ or error
        """
        key = settings.KIMI_API_KEY
        if not key or len(key) < 10:
            return [
                error_card(
                    "Kimi API", "🌙", "Missing/Invalid Key", error_type="missing_config"
                )
            ]

        try:
            resp = await client.get(
                "https://api.moonshot.cn/v1/users/me/balance",
                headers={"Authorization": f"Bearer {key}"},
            )

            if resp.status_code == 401:
                return [
                    error_card(
                        "Kimi API", "🌙", "Unauthorized", error_type="auth_failed"
                    )
                ]
            if resp.status_code != 200:
                return [
                    error_card(
                        "Kimi API",
                        "🌙",
                        f"HTTP {resp.status_code}",
                        error_type="api_error",
                    )
                ]

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
        except httpx.RequestError:
            return [
                error_card("Kimi API", "🌙", "Connection Failed", error_type="timeout")
            ]
        except (ValueError, KeyError, TypeError):
            return [
                error_card(
                    "Kimi API", "🌙", "Invalid Response", error_type="parse_error"
                )
            ]
