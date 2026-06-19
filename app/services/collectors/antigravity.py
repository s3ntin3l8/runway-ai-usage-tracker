"""
Antigravity CLI quota collector.

Fetches 4 quota gauges (Gemini-pool weekly/5h + Frontier-pool weekly/5h) from
the Code Assist cloud API using the agy OAuth token.
"""

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import error_card
from app.services.collectors.antigravity_api import AntigravityApiMixin
from app.services.collectors.antigravity_oauth import AntigravityOAuthMixin
from app.services.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


class AntigravityCollector(
    AntigravityOAuthMixin,
    AntigravityApiMixin,
    BaseCollector,
):
    """Orchestrator for Antigravity (agy) quota collection."""

    PROVIDER_ID = "antigravity"
    DEFAULT_WINDOW_TYPE = "weekly"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "api": ("API (api)", "_strategy_api_wrap"),
    }

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(
            provider_name="Antigravity",
            credentials_path=settings.ANTIGRAVITY_OAUTH_PATH,
            account_id=account_id,
            account_label=account_label,
        )

    async def is_configured(self) -> bool:
        return bool(await self._get_current_token())

    def _fallback_strategies(self) -> list[Any]:
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        return await self._collect_via_api(client)

    async def _strategy_api_wrap(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        return await self._collect_via_api(client)

    async def _error_handler(self) -> list[dict[str, Any]]:
        creds = await self._get_credentials()
        if not creds:
            return [
                error_card(
                    "Antigravity",
                    "🛸",
                    "No credentials found — run `agy` at least once",
                    error_type="missing_config",
                )
            ]
        return [error_card("Antigravity", "🛸", "API strategy failed", error_type="api_error")]
