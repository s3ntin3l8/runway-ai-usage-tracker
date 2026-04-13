"""
Google Gemini quota collector orchestrating API and log fallback strategies.
"""

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector
from app.services.collectors.gemini_api import GeminiApiMixin
from app.services.collectors.gemini_local import GeminiLocalMixin

# Mixins
from app.services.collectors.gemini_oauth import GeminiOAuthMixin
from app.services.credential_provider import credential_provider

logger = logging.getLogger(__name__)


class GeminiCollector(
    GeminiOAuthMixin,
    GeminiApiMixin,
    GeminiLocalMixin,
    BaseCollector,
):
    """
    Orchestrator for Gemini data collection.
    Inherits from GeminiOAuthMixin for token logic and other mixins for strategies.
    """

    PROVIDER_ID = "gemini"
    DEFAULT_WINDOW_TYPE = "daily"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        """Initialize orchestrator."""
        # Find credentials via centralized provider
        credentials_path = credential_provider.get_gemini_credentials_path()
        if not credentials_path:
            credentials_path = settings.GEMINI_OAUTH_PATH

        super().__init__(
            provider_name="Gemini",
            credentials_path=credentials_path,
            account_id=account_id,
            account_label=account_label,
        )

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for Gemini (Logs)."""
        return [
            self._collect_via_logs,
        ]

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """API strategy."""
        return await self._collect_via_api(client)

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return final error card context when both API and logs fail."""
        # Check if we have credentials to determine the most helpful error
        creds = await self._get_credentials()
        if not creds:
            return [
                error_card(
                    "Gemini",
                    "🔵",
                    "No credentials found",
                    error_type="missing_config",
                )
            ]

        return [
            error_card("Gemini", "🔵", "All collection strategies failed", error_type="api_error")
        ]
