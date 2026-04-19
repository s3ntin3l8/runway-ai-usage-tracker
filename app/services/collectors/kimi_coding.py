"""
Kimi Coding (IDE) quota collector with weekly and rate limits.

Collection Strategy:
- Endpoint: POST https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages
- Authentication priority:
  1. KIMI_AUTH_TOKEN environment variable
  2. Chrome cookie 'kimi-auth' (via get_kimi_auth_cookie())
- Returns weekly quota + 5-hour rate limit
- Two cards: Weekly quota + Rate limit window

Membership Tiers:
- Andante (¥49/mo): 1,024 requests/week
- Moderato (¥99/mo): 2,048 requests/week
- Allegretto (¥199/mo): 7,168 requests/week
- All tiers: 200 requests / 5 hours rate limit

API Response Format:
{
  "usages": [{
    "scope": "FEATURE_CODING",
    "detail": {
      "limit": "2048",
      "used": "214",
      "remaining": "1834",
      "resetTime": "2026-01-09T15:23:13.716839300Z"
    },
    "limits": [{
      "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
      "detail": {
        "limit": "200",
        "used": "139",
        "remaining": "61",
        "resetTime": "2026-01-06T13:33:02.717479433Z"
      }
    }]
  }]
}

See Also:
- kimi_api.py for API balance (different service)

Error Handling:
- No auth: Returns error card
- API errors: Returns error card
- Invalid response: Returns error card
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.browser_cookies import get_kimi_auth_cookie
from app.core.config import settings
from app.core.utils import error_card, human_delta
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider


class KimiCodingCollector(BaseCollector):
    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)

    async def is_configured(self) -> bool:
        """Check if Kimi Coding auth token or cookie is present."""
        return self._is_valid_credential(await self._get_auth_token())

    """Collector for Kimi Coding IDE quotas (weekly + rate limits)."""

    PROVIDER_ID = "kimi_coding"
    DEFAULT_WINDOW_TYPE = "weekly"

    API_ENDPOINT = "https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages"

    def _fallback_strategies(self) -> list[Any]:
        """Return the strategy list for Kimi Coding."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect Kimi Coding usage via API."""
        return await self._strategy_api(client)

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return fallback error when API fails."""
        token = await self._get_auth_token()
        if not token:
            return [
                error_card(
                    "Kimi Coding",
                    "🌙",
                    "No Auth (set KIMI_AUTH_TOKEN or login in Chrome)",
                    error_type="missing_config",
                )
            ]
        return [error_card("Kimi Coding", "🌙", "API Collection Failed", error_type="api_error")]

    async def _strategy_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect Kimi Coding usage via API."""
        token = await self._get_auth_token()
        if not token:
            return []

        try:
            resp = await client.post(
                self.API_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    "Referer": "https://www.kimi.com/",
                    "Origin": "https://www.kimi.com",
                },
                json={"scope": ["FEATURE_CODING"]},
                timeout=10.0,
            )

            if resp.status_code != 200:
                return []

            data = resp.json()
            return self._parse_response(data)
        except (httpx.RequestError, ValueError, KeyError, TypeError):
            return []

    async def _get_auth_token(self) -> str | None:
        """
        Get authentication token from env var or Chrome cookie.

        Priority:
        1. KIMI_AUTH_TOKEN environment variable
        2. Chrome cookie 'kimi-auth'

        Returns:
            Token string or None
        """
        # Priority 1: DB-stored session cookie (manual override set via settings UI)
        db_token = credential_provider.get_provider_session_cookie("kimi_coding")
        if db_token:
            return db_token

        # Priority 2: Environment variable
        token = settings.KIMI_AUTH_TOKEN
        if token:
            return token

        # Priority 3: Chrome cookie
        return await asyncio.to_thread(get_kimi_auth_cookie)

    def _parse_response(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Parse API response into quota cards.

        Args:
            data: API response dict

        Returns:
            List of 2 cards (weekly + rate limit) or error
        """
        usages = data.get("usages", [])
        if not usages:
            return [
                {
                    "service_name": "Kimi Coding",
                    "icon": "🌙",
                    "remaining": "No active plan",
                    "unit": "quota",
                    "reset": "—",
                    "health": "good",
                    "pace": "N/A",
                    "detail": "No active plan",
                    "data_source": "api",
                    "is_unlimited": False,
                    "unit_type": "unknown",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ]

        # Get first FEATURE_CODING usage or first available
        usage = None
        for u in usages:
            if u.get("scope") == "FEATURE_CODING":
                usage = u
                break
        if not usage:
            usage = usages[0]

        cards = []

        # Card 1: Weekly quota
        weekly = usage.get("detail", {})
        if weekly:
            card = self._parse_weekly_quota(weekly)
            if card:
                cards.append(card)

        # Card 2: Rate limit (5-hour window)
        limits = usage.get("limits", [])
        if limits:
            rate_limit = limits[0].get("detail", {})
            window = limits[0].get("window", {})
            if rate_limit:
                card = self._parse_rate_limit(rate_limit, window)
                if card:
                    cards.append(card)

        return (
            cards
            if cards
            else [error_card("Kimi Coding", "🌙", "No Quota Data", error_type="parse_error")]
        )

    def _parse_weekly_quota(self, detail: dict[str, Any]) -> dict[str, Any] | None:
        """Parse weekly quota into card."""
        try:
            limit = int(detail.get("limit", 0))
            used = int(detail.get("used", 0))
            remaining = int(detail.get("remaining", limit - used))
            reset_str = detail.get("resetTime", "")

            if limit == 0:
                return None

            pct_used = (used / limit * 100) if limit > 0 else 0

            # Parse reset time
            reset_delta = "Unknown"
            reset_dt = None
            if reset_str:
                try:
                    # ISO format with possible microseconds
                    reset_dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
                    reset_delta = human_delta(reset_dt)
                except (ValueError, TypeError):
                    pass

            # Detect tier from limit
            tier = self._detect_tier(limit)

            return {
                "service_name": "Kimi Coding (Weekly)",
                "icon": "🌙",
                "remaining": f"{remaining}",
                "unit": f"{limit} req",
                "reset": reset_delta,
                "health": ("good" if pct_used < 50 else "warning" if pct_used < 80 else "critical"),
                "pace": tier,
                "detail": f"{used} used · {tier}",
                "used_value": float(used),
                "limit_value": float(limit),
                "is_unlimited": False,
                "unit_type": "requests",
                "reset_at": reset_dt.isoformat() if reset_dt else None,
                "data_source": "api",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        except (ValueError, TypeError):
            return None

    def _parse_rate_limit(
        self, detail: dict[str, Any], window: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Parse rate limit (5-hour window) into card."""
        try:
            limit = int(detail.get("limit", 0))
            used = int(detail.get("used", 0))
            remaining = int(detail.get("remaining", limit - used))
            reset_str = detail.get("resetTime", "")

            if limit == 0:
                return None

            pct_used = (used / limit * 100) if limit > 0 else 0

            # Parse reset time
            reset_delta = "Unknown"
            reset_dt = None
            if reset_str:
                try:
                    reset_dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
                    reset_delta = human_delta(reset_dt)
                except (ValueError, TypeError):
                    pass

            # Get window duration
            duration = window.get("duration", 300)  # Default 5 hours in minutes
            window_label = f"{duration // 60}h" if duration >= 60 else f"{duration}m"

            return {
                "service_name": f"Kimi Coding ({window_label})",
                "icon": "⏱️",
                "remaining": f"{remaining}",
                "unit": f"{limit} req",
                "reset": reset_delta,
                "health": ("good" if pct_used < 70 else "warning" if pct_used < 90 else "critical"),
                "pace": ("Stable" if pct_used < 50 else "High" if pct_used < 80 else "Critical"),
                "detail": f"{used} used · Rate limit window",
                "used_value": float(used),
                "limit_value": float(limit),
                "is_unlimited": False,
                "unit_type": "requests",
                "reset_at": reset_dt.isoformat() if reset_dt else None,
                "data_source": "api",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        except (ValueError, TypeError):
            return None

    def _detect_tier(self, limit: int) -> str:
        """
        Detect membership tier from weekly quota limit.

        Args:
            limit: Weekly request limit

        Returns:
            Tier name
        """
        if limit >= 7000:
            return "Allegretto"
        if limit >= 2000:
            return "Moderato"
        if limit >= 1000:
            return "Andante"
        return "Basic"
