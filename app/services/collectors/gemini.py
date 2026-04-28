"""
Google Gemini quota collector orchestrating API and log fallback strategies.
"""

import logging
from datetime import UTC, datetime
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

    def _enrich_results(
        self,
        primary: list[dict[str, Any]] | None,
        enrichment: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge local enrichment into API primary cards by model class."""
        if not enrichment or self._is_error_result(enrichment):
            return primary or []

        if not primary or self._is_error_result(primary):
            return primary or []

        e_data = enrichment[0]
        messages = e_data.get("_messages", [])
        all_by_model = e_data.get("by_model", {})
        detail_suffix = e_data.get("_enrichment_detail", "")

        for card in primary:
            if card.get("data_source") == self.DATA_SOURCE_API:
                model_id = card.get("model_id", "unknown")
                reset_at = card.get("reset_at")

                model_messages = [
                    m for m in messages if m["model_class"] == model_id and m["timestamp"]
                ]

                if reset_at:
                    reset_dt = self._parse_timestamp(reset_at)
                    window_messages = [
                        m
                        for m in model_messages
                        if (self._parse_timestamp(m["timestamp"]) is not None)
                        and (self._parse_timestamp(m["timestamp"]) < reset_dt)
                    ]
                else:
                    window_messages = model_messages

                if window_messages:
                    class_data = self._aggregate_window_messages(window_messages)
                    token_usage = {
                        "input": class_data.get("input", 0),
                        "output": class_data.get("output", 0),
                        "reasoning": class_data.get("reasoning", 0),
                        "cache_read": class_data.get("cache_read", 0),
                        "total": class_data.get("total", 0),
                    }
                    card["token_usage"] = token_usage
                    card["msgs"] = class_data.get("session_count", 0)
                    card["pct_used"] = 0

                    used = card.get("used_value", 0)
                    limit = card.get("limit_value", 1)
                    if limit and limit > 0:
                        card["pct_used"] = (used / limit) * 100

                if all_by_model:
                    filtered_by_model = {}
                    for model_name, model_info in all_by_model.items():
                        model_class = self._map_model_to_class(model_name)
                        if model_class == model_id:
                            filtered_by_model[model_name] = model_info
                    if filtered_by_model:
                        card["by_model"] = filtered_by_model

                if detail_suffix:
                    card["detail"] = f"{card.get('detail', '')} | {detail_suffix}"

        return primary

    def _aggregate_window_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate tokens from filtered messages."""
        totals = {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache_read": 0,
            "total": 0,
            "session_count": 0,
        }
        for msg in messages:
            tokens = msg.get("tokens", {})
            totals["input"] += tokens.get("input", 0)
            totals["output"] += tokens.get("output", 0)
            totals["reasoning"] += tokens.get("thoughts", 0)
            totals["cache_read"] += tokens.get("cached", 0)
            totals["total"] += tokens.get("total", 0)
            totals["session_count"] += 1
        return totals

    @staticmethod
    def _parse_timestamp(value: str | int | float | None) -> datetime | None:
        """Normalize a timestamp to a timezone-aware datetime."""
        if value is None:
            return None
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value, tz=UTC)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
        return None
