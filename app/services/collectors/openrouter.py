import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import HealthCalculator, http_request_with_retry
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterCollector(BaseCollector):
    """
    Collector for OpenRouter usage, credits, and per-key limits.
    Uses: https://openrouter.ai/api/v1/credits  (account-level)
          https://openrouter.ai/api/v1/key      (per-key spending limit, best-effort)
    """

    PROVIDER_ID = "openrouter"
    DEFAULT_WINDOW_TYPE = "monthly"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)

    async def _get_api_key(self) -> str | None:
        """Discover API key: DB (UI-set) → token cache → env var."""
        db_key = credential_provider.get_provider_api_key("openrouter")
        if db_key:
            return db_key

        if self.account_id:
            token = await token_cache.get_token("openrouter", "api_key", account_id=self.account_id)
            if token:
                return token

        return settings.OPENROUTER_API_KEY or None

    async def is_configured(self) -> bool:
        """Check if OpenRouter API key is present."""
        return self._is_valid_credential(await self._get_api_key())

    def _build_headers(self) -> dict[str, str]:
        """Build request headers including optional OpenRouter attribution."""
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if settings.OPENROUTER_HTTP_REFERER:
            headers["HTTP-Referer"] = settings.OPENROUTER_HTTP_REFERER
        if settings.OPENROUTER_X_TITLE:
            headers["X-Title"] = settings.OPENROUTER_X_TITLE
        return headers

    async def _key_endpoint_request(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> httpx.Response | None:
        """Make best-effort request to key endpoint with 1s timeout."""
        try:
            return await client.get(f"{OPENROUTER_BASE_URL}/key", headers=headers, timeout=1)
        except Exception as e:
            logger.debug(f"OpenRouter key API best-effort fetch failed: {e}")
            return None

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect usage data from OpenRouter via Credits + Key APIs."""
        api_key = await self._get_api_key()
        if not api_key:
            return []

        self._api_key = api_key
        headers = self._build_headers()
        cards: list[dict[str, Any]] = []

        try:
            credits_resp = await http_request_with_retry(
                client, "GET", f"{OPENROUTER_BASE_URL}/credits", headers=headers, timeout=10
            )

            if credits_resp.status_code != 200:
                logger.error(
                    f"OpenRouter credits API error (HTTP {credits_resp.status_code}): {credits_resp.text}"
                )
                return []

            credits_data = credits_resp.json()
            if "data" in credits_data:
                info = credits_data["data"]
                total_credits = info.get("total_credits", 0.0)
                usage = info.get("usage", 0.0)
                remaining = max(0, total_credits - usage)

                cards.append(
                    {
                        "service_name": "OpenRouter Credits",
                        "icon": "🚀",
                        "remaining": f"${remaining:.2f}",
                        "unit": "USD",
                        "reset": "Prepaid",
                        "health": HealthCalculator.from_balance(remaining),
                        "pace": "Stable",
                        "detail": f"Used: ${usage:.2f} of ${total_credits:.2f} [API]",
                        "used_value": usage,
                        "limit_value": total_credits,
                        "unit_type": "currency",
                        "data_source": "api",
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )
        except Exception as e:
            logger.error(f"Failed to collect OpenRouter credits: {e}")
            return []

        try:
            key_resp = await self._key_endpoint_request(client, headers)

            if key_resp and key_resp.status_code == 200:
                key_data = key_resp.json()
                key_info = key_data.get("data", {})
                key_limit = key_info.get("limit")
                key_usage = key_info.get("usage", 0.0)

                if key_limit is not None and key_limit > 0:
                    key_remaining = max(0, key_limit - key_usage)
                    cards.append(
                        {
                            "service_name": "OpenRouter Key Limit",
                            "icon": "🔑",
                            "remaining": f"${key_remaining:.2f}",
                            "unit": "USD",
                            "reset": "Per-key",
                            "health": HealthCalculator.from_spend(key_usage, key_limit),
                            "pace": "Stable",
                            "detail": f"Key used: ${key_usage:.2f} of ${key_limit:.2f} [API]",
                            "used_value": key_usage,
                            "limit_value": key_limit,
                            "unit_type": "currency",
                            "data_source": "api",
                            "updated_at": datetime.now(UTC).isoformat(),
                        }
                    )
            elif key_resp:
                logger.debug(
                    f"OpenRouter key API non-200 (HTTP {key_resp.status_code}), skipping key card"
                )
        except Exception as e:
            logger.debug(f"OpenRouter key API best-effort fetch failed: {e}")

        return cards

    def _fallback_strategies(self) -> list[Any]:
        """Return an ordered list of fallback async methods. Currently none for OpenRouter."""
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return the ultimate error card(s) when all strategies fail."""
        from app.core.utils import error_card

        api_key = await self._get_api_key()
        if not api_key:
            return [
                error_card(
                    "OpenRouter", "🚀", "Missing OPENROUTER_API_KEY", error_type="missing_config"
                )
            ]

        return [error_card("OpenRouter", "🚀", "API connection failed", error_type="api_error")]
