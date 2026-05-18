"""
ChatGPT Codex quota collector orchestrating Web Gateway / OAuth strategies.

Local CLI / log enrichment has moved to the sidecar; this server-side collector
only handles HTTP-based strategies.
"""

import logging
import uuid
from typing import Any

import httpx

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import error_card
from app.services.collectors.base import BaseCollector

# Mixins
from app.services.collectors.chatgpt_oauth import ChatGPTWebOAuthMixin
from app.services.collectors.chatgpt_web import ChatGPTWebMixin

logger = logging.getLogger(__name__)


class ChatGPTCollector(
    ChatGPTWebOAuthMixin,
    ChatGPTWebMixin,
    BaseCollector,
):
    """
    Orchestrator for ChatGPT data collection.
    Inherits from mixins for auth and HTTP API strategies.
    """

    PROVIDER_ID = "chatgpt"
    DEFAULT_WINDOW_TYPE = "weekly"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "web": ("Web API (web)", "_strategy_web_wrap"),
    }

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        """Initialize orchestrator."""
        super().__init__(account_id=account_id, account_label=account_label)

        # In-memory session state for mixins
        self._refreshed_token = None
        self._refreshed_token_expiry = None
        self._device_id = str(uuid.uuid4())

    def _capture_primary_metadata(self, primary: list[dict[str, Any]]) -> None:
        """
        Extract reset_at from primary cards so enrichment can align its window boundaries.
        """

        self._primary_reset_at = None
        for card in primary:
            reset_at_str = card.get("reset_at")
            if not reset_at_str:
                continue
            try:
                dt = parse_iso8601_utc(reset_at_str)
                self._primary_reset_at = dt
                break  # We only need one reset_at for ChatGPT
            except (ValueError, TypeError):
                continue

    async def is_configured(self) -> bool:
        """Check if ChatGPT auth data (OAuth token or session cookie) is present."""
        # Use None for client to avoid triggering background refreshes during config check
        auth = await self._get_auth_data(None)
        return bool(auth.get("token"))

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for ChatGPT (HTTP only)."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Web API / OAuth strategy."""
        auth = await self._get_auth_data(client)
        token = auth.get("token")
        account_id = auth.get("account_id")

        if not token:
            return []

        try:
            return await self._fetch_api_data(
                client,
                token,
                account_id,
                auth.get("source", "oauth"),
                input_source=auth.get("input_source", "server"),
            )
        except Exception as e:
            logger.debug(f"ChatGPT Web API failed: {e}")
        return []

    async def _strategy_web_wrap(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Dispatch wrapper: Web API / OAuth strategy."""
        return await self._primary_strategy(client)

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return final error card."""
        return [
            error_card("ChatGPT Codex", "💬", "No logs/auth found", error_type="missing_config")
        ]
