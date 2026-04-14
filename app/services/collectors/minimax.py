import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider

logger = logging.getLogger(__name__)


class MiniMaxCollector(BaseCollector):
    """
    Collector for MiniMax (Coding Plan) usage.

    Supports:
    - API token (preferred): https://api.minimax.io/v1/coding_plan/remains
    - HTML fallback: /user-center/payment/coding-plan page parsing
    - Manual cookie override

    Config:
    - MINIMAX_API_KEY: API token (env var or DB)
    - MINIMAX_HOST: Override host (default: minimax.io, China: minimaxi.com)
    - MINIMAX_COOKIE: Manual cookie header for fallback
    """

    PROVIDER_ID = "minimax"
    DEFAULT_WINDOW_TYPE = "monthly"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self.api_key: str | None = None
        self.host: str = ""

    def _get_api_key(self) -> str | None:
        """DB (UI-set) → env var."""
        return credential_provider.get_provider_api_key("minimax") or settings.MINIMAX_API_KEY or None

    def _get_session_cookie(self) -> str | None:
        """DB (UI-set) → env var."""
        return credential_provider.get_provider_session_cookie("minimax") or settings.MINIMAX_COOKIE or None

    def _get_host(self) -> str:
        """Get host from config or default."""
        if settings.MINIMAX_HOST:
            return settings.MINIMAX_HOST
        return "minimax.io"

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect usage data from MiniMax via API."""
        self.api_key = self._get_api_key()
        self.host = self._get_host()

        if not self.api_key:
            return []

        return await self._fetch_via_api(client)

    async def _fetch_via_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fetch usage via the API endpoint."""
        base_url = f"https://api.{self.host}"

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            resp = await client.get(
                f"{base_url}/v1/coding_plan/remains",
                headers=headers,
                timeout=10,
            )

            if resp.status_code == 200:
                data = resp.json()
                return self._parse_api_response(data)

            logger.error(f"MiniMax API error (HTTP {resp.status_code}): {resp.text[:200]}")

        except Exception as e:
            logger.error(f"Failed to collect MiniMax usage: {e}")

        return []

    def _parse_api_response(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse API response and build cards."""
        now_str = datetime.now(UTC).isoformat()

        model_remains = data.get("model_remains", [])

        if not model_remains:
            return [
                {
                    "service_name": "MiniMax",
                    "icon": "🤖",
                    "remaining": "No active plan",
                    "unit": "quota",
                    "reset": "—",
                    "health": "good",
                    "pace": "N/A",
                    "detail": "No active plan",
                    "data_source": "api",
                    "is_unlimited": False,
                    "unit_type": "unknown",
                    "updated_at": now_str,
                }
            ]

        results = []
        for item in model_remains:
            name = item.get("model_name", "Unknown")
            remains = item.get("remains", 0)

            results.append(
                {
                    "service_name": f"MiniMax: {name}",
                    "icon": "🤖",
                    "remaining": f"{remains:,}",
                    "unit": "requests",
                    "reset": "Coding Plan",
                    "health": "good"
                    if remains > 100
                    else "warning"
                    if remains > 20
                    else "critical",
                    "pace": "Active",
                    "detail": f"{name} quota [API]",
                    "used_value": 0.0,
                    "limit_value": float(remains),
                    "unit_type": "count",
                    "data_source": "api",
                    "updated_at": now_str,
                }
            )

        return results

    def _fallback_strategies(self) -> list[Any]:
        """Return fallback strategies."""
        return [self._fetch_via_html]

    async def _fetch_via_html(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fallback: Parse HTML from coding plan page."""
        session_cookie = self._get_session_cookie()
        api_key = self.api_key or self._get_api_key()

        if not session_cookie and not api_key:
            return []

        self.host = self._get_host()
        base_url = f"https://platform.{self.host}"

        try:
            headers = {
                "Accept": "text/html",
                "User-Agent": "Mozilla/5.0",
            }

            if session_cookie:
                headers["Cookie"] = session_cookie
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            resp = await client.get(
                f"{base_url}/user-center/payment/coding-plan",
                headers=headers,
                timeout=10,
            )

            if resp.status_code == 200:
                html = resp.text
                return self._parse_html_response(html)

            logger.debug(f"MiniMax HTML fallback failed (HTTP {resp.status_code})")

        except Exception as e:
            logger.debug(f"MiniMax HTML fallback error: {e}")

        return []

    def _parse_html_response(self, html: str) -> list[dict[str, Any]]:
        """Parse HTML response to extract usage info."""
        import re

        now_str = datetime.now(UTC).isoformat()

        avail_usage_match = re.search(r"Available usage[:\s]*(\d+)", html, re.IGNORECASE)
        plan_match = re.search(r"Coding Plan[:\s]*([^\n<]+)", html, re.IGNORECASE)
        reset_match = re.search(r"Resets? in[:\s]*([^\n<]+)", html, re.IGNORECASE)

        if not avail_usage_match:
            return [
                {
                    "service_name": "MiniMax",
                    "icon": "🤖",
                    "remaining": "No active plan",
                    "unit": "quota",
                    "reset": "—",
                    "health": "good",
                    "pace": "N/A",
                    "detail": "No active plan",
                    "data_source": "html",
                    "is_unlimited": False,
                    "unit_type": "unknown",
                    "updated_at": now_str,
                }
            ]

        remains = int(avail_usage_match.group(1))
        plan_name = plan_match.group(1).strip() if plan_match else "Coding Plan"
        reset_text = reset_match.group(1).strip() if reset_match else "Monthly"

        return [
            {
                "service_name": "MiniMax",
                "icon": "🤖",
                "remaining": f"{remains:,}",
                "unit": "requests",
                "reset": reset_text,
                "health": "good"
                if remains > 100
                else "warning"
                if remains > 20
                else "critical",
                "pace": "Active",
                "detail": f"{plan_name} [HTML]",
                "used_value": 0.0,
                "limit_value": float(remains),
                "unit_type": "count",
                "data_source": "html",
                "updated_at": now_str,
            }
        ]

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return the ultimate error card(s) when all strategies fail."""
        from app.core.utils import error_card

        if not self.api_key and not self._get_session_cookie():
            return [
                error_card("MiniMax", "🤖", "Missing API key or cookie", error_type="missing_config")
            ]

        return [error_card("MiniMax", "🤖", "API connection failed", error_type="api_error")]
