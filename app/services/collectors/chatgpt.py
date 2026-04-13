"""
ChatGPT Codex quota collector orchestrating API and log fallback strategies.
"""

import logging
import uuid
from typing import Any

import httpx

from app.core.utils import error_card
from app.services.collectors.base import BaseCollector
from app.services.collectors.chatgpt_api import ChatGPTApiMixin

# Mixins
from app.services.collectors.chatgpt_auth import ChatGPTAuthMixin
from app.services.collectors.chatgpt_local import ChatGPTLocalMixin

logger = logging.getLogger(__name__)


class ChatGPTCollector(
    ChatGPTAuthMixin,
    ChatGPTApiMixin,
    ChatGPTLocalMixin,
    BaseCollector,
):
    """
    Orchestrator for ChatGPT data collection.
    Inherits from mixins for auth, API, and local strategies.
    """

    PROVIDER_ID = "chatgpt"
    DEFAULT_WINDOW_TYPE = "daily"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        """Initialize orchestrator."""
        super().__init__(account_id=account_id, account_label=account_label)
        
        # In-memory session state for mixins
        self._refreshed_token = None
        self._refreshed_token_expiry = None
        self._device_id = str(uuid.uuid4())

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for ChatGPT."""
        return [
            self._collect_via_cli_rpc,
            self._strategy_local_logs,
        ]

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Web API / OAuth strategy."""
        auth = await self._get_auth_data(client)
        token = auth.get("token")
        account_id = auth.get("account_id")

        if not token:
            return []

        try:
            return await self._fetch_api_data(
                client, token, account_id, auth.get("source", "oauth")
            )
        except Exception as e:
            logger.debug(f"ChatGPT Web API failed: {e}")
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return final error card."""
        return [
            error_card(
                "ChatGPT Codex", "💬", "No logs/auth found", error_type="missing_config"
            )
        ]
