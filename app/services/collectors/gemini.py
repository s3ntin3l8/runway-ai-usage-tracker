"""
Google Gemini quota collector orchestrating API and log fallback strategies.
"""

import logging
from datetime import datetime
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

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "api": ("API (api)", "_strategy_api_wrap"),
        "local": ("Local Logs / CLI (local)", "_collect_via_logs", {"enrich": True}),
    }

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

    def _capture_primary_metadata(self, primary: list[dict[str, Any]]) -> None:
        """
        Extract reset_at per model_id from primary API cards so enrichment
        can align window boundaries instead of using fixed cutoffs.
        """
        resets: dict[str, datetime] = {}
        for card in primary:
            reset_at = card.get("reset_at")
            if not reset_at:
                continue
            try:
                dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                mid = card.get("model_id", "unknown")
                resets[mid] = dt
            except (ValueError, TypeError):
                continue
        self._window_resets = resets

    async def is_configured(self) -> bool:
        """Check if Gemini credentials or logs are present."""
        if await self._get_current_token():
            return True
        # Check logs/CLI
        if await self._collect_via_logs(None):
            return True
        return False

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for Gemini (Logs)."""
        return [
            self._collect_via_logs,
        ]

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """API strategy."""
        return await self._collect_via_api(client)

    async def _strategy_api_wrap(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Dispatch wrapper: API (OAuth) strategy."""
        return await self._collect_via_api(client)

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return final error card context when both API and logs fail."""
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
