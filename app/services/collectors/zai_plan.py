"""
zAI Plan (Quota) collector with token and time limit tracking.

Collection Strategy:
- Requires ZAI_API_KEY environment variable (same as zai_api)
- Primary endpoint: https://api.z.ai/api/monitor/usage/quota/limit
- Fallback endpoint: https://open.bigmodel.cn/api/monitor/usage/quota/limit
- Returns quota limits (TOKENS_LIMIT, TIME_LIMIT)
- Multiple cards: one per limit type (typically 2 cards)

API Response Format:
{
  "data": {
    "planName": "Basic Plan",
    "limits": [
      {
        "type": "TOKENS_LIMIT",
        "limit": 1000000,
        "used": 450000,
        "nextResetTime": 1775570736000
      },
      {
        "type": "TIME_LIMIT",
        "limit": 3600,
        "used": 1800,
        "nextResetTime": 1775570736000
      }
    ]
  }
}

See Also:
- zai_api.py for prepaid balance (different metric)

Error Handling:
- Missing key: Returns error card
- API errors: Returns error card with status
- Invalid response: Returns error card
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import httpx
from app.core.config import settings
from app.core.utils import error_card, human_delta
from app.services.collectors.base import BaseCollector


class ZaiPlanCollector(BaseCollector):
    """Collector for zAI Plan quota limits (tokens and time windows)."""

    # API endpoints in priority order
    API_ENDPOINTS = [
        "https://api.z.ai/api/monitor/usage/quota/limit",
        "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
    ]

    def _fallback_strategies(self) -> List[Any]:
        """Return the strategy list for zAI Plan."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect zAI plan quota limits trying multiple endpoints."""
        return await self._strategy_api(client)

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return fallback error when all endpoints fail."""
        key = settings.ZAI_API_KEY
        if not key or key.lower() == "zai":
            return [
                error_card(
                    "zAI Plan", "📊", "Missing/Invalid Key", error_type="missing_config"
                )
            ]
        return [error_card("zAI Plan", "📊", "API Unavailable", error_type="api_error")]

    async def _strategy_api(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Collect zAI plan quota limits trying multiple endpoints."""
        key = settings.ZAI_API_KEY
        if not key or key.lower() == "zai":
            return []

        for endpoint in self.API_ENDPOINTS:
            try:
                result = await self._fetch_quota(client, key, endpoint)
                if result:
                    # If it's an error result, we continue to next endpoint if available
                    if not self._is_error_result(result):
                        return result
            except Exception:
                continue
        
        return []


    async def _fetch_quota(
        self, client: httpx.AsyncClient, key: str, endpoint: str
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch quota from a specific endpoint.

        Args:
            client: HTTP client
            key: API key
            endpoint: URL to query

        Returns:
            List of cards or None if endpoint failed
        """
        resp = await client.get(
            endpoint,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=10.0,
        )

        if resp.status_code != 200:
            return None

        data = resp.json()
        plan_data = data.get("data", {})
        plan_name = (
            plan_data.get("planName")
            or plan_data.get("plan")
            or plan_data.get("packageName", "Unknown")
        )
        limits = plan_data.get("limits", [])

        if not limits:
            return [
                error_card(
                    "zAI Plan", "📊", "No Limits Found", error_type="parse_error"
                )
            ]

        cards = []
        for limit in limits:
            card = self._parse_limit(limit, plan_name)
            if card:
                cards.append(card)

        return cards if cards else None

    def _parse_limit(
        self, limit: Dict[str, Any], plan_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Parse a single limit entry into a card.

        Args:
            limit: Limit dict from API
            plan_name: Name of the plan

        Returns:
            Card dict or None if invalid
        """
        limit_type = limit.get("type", "")
        limit_val = limit.get("limit", 0)
        used_val = limit.get("used", 0)
        reset_ts = limit.get("nextResetTime")

        # Skip invalid entries
        if not limit_val:
            return None

        # Calculate remaining
        remaining = max(0, limit_val - used_val)
        pct_used = (used_val / limit_val * 100) if limit_val > 0 else 0

        # Determine label and formatting based on type
        if limit_type == "TOKENS_LIMIT":
            service = "zAI Plan (Tokens)"
            remaining_str = f"{remaining:,}"
            unit = f"{limit_val:,} limit"
            detail = f"{used_val:,} used · {plan_name}"
        elif limit_type == "TIME_LIMIT":
            service = "zAI Plan (Time)"
            remaining_str = f"{remaining}"
            unit = f"{limit_val} min"
            detail = f"{used_val} min used · {plan_name}"
        else:
            # Unknown limit type
            service = f"zAI Plan ({limit_type})"
            remaining_str = f"{remaining}"
            unit = f"{limit_val} limit"
            detail = f"{used_val} used · {plan_name}"

        # Parse reset time
        reset_str = "Manual"
        reset_at = None
        if reset_ts:
            try:
                # Handle both milliseconds and seconds
                if reset_ts > 1000000000000:  # Milliseconds
                    reset_ts = reset_ts / 1000
                reset_dt = datetime.fromtimestamp(reset_ts, tz=timezone.utc)
                reset_str = human_delta(reset_dt)
                reset_at = reset_dt.isoformat()
            except (ValueError, OSError, OverflowError):
                reset_str = "Unknown"

        # Health based on percentage used
        if pct_used < 50:
            health = "good"
        elif pct_used < 80:
            health = "warning"
        else:
            health = "critical"

        # Determine unit_type based on limit_type
        unit_type = (
            "tokens"
            if limit_type == "TOKENS_LIMIT"
            else "minutes" if limit_type == "TIME_LIMIT" else "generic"
        )

        return {
            "service": service,
            "icon": "📊",
            "remaining": remaining_str,
            "unit": unit,
            "reset": reset_str,
            "health": health,
            "pace": (
                "Stable" if pct_used < 50 else "High" if pct_used < 80 else "Critical"
            ),
            "detail": detail,
            "used_value": float(used_val),
            "limit_value": float(limit_val),
            "is_unlimited": False,
            "unit_type": unit_type,
            "reset_at": reset_at,
            "data_source": "api",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
