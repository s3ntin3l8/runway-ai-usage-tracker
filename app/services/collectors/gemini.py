"""
Google Gemini quota collector orchestrating API strategy.

Local CLI / log fallback has moved to the sidecar; this server-side collector
only handles HTTP-based strategies.
"""

import logging
from datetime import datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.date_utils import parse_iso8601_utc
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector
from app.services.collectors.gemini_api import GeminiApiMixin

# Mixins
from app.services.collectors.gemini_oauth import GeminiOAuthMixin
from app.services.credential_provider import credential_provider

logger = logging.getLogger(__name__)


class GeminiCollector(
    GeminiOAuthMixin,
    GeminiApiMixin,
    BaseCollector,
):
    """
    Orchestrator for Gemini data collection.
    Inherits from GeminiOAuthMixin for token logic and GeminiApiMixin for strategies.
    """

    PROVIDER_ID = "gemini"
    DEFAULT_WINDOW_TYPE = "daily"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "api": ("API (api)", "_strategy_api_wrap"),
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
                dt = parse_iso8601_utc(reset_at)
                mid = card.get("model_id", "unknown")
                resets[mid] = dt
            except (ValueError, TypeError):
                continue
        self._window_resets = resets

    async def is_configured(self) -> bool:
        """Check if Gemini credentials are present."""
        return bool(await self._get_current_token())

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for Gemini (HTTP only)."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """API strategy."""
        return await self._collect_via_api(client)

    async def _strategy_api_wrap(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Dispatch wrapper: API (OAuth) strategy."""
        return await self._collect_via_api(client)

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return final error card context when the API strategy fails."""
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

        return [error_card("Gemini", "🔵", "API strategy failed", error_type="api_error")]
