"""
ChatGPT Codex quota collector orchestrating API and log fallback strategies.
"""

import logging
import uuid
from typing import Any

import httpx

from app.core.utils import error_card
from app.services.collectors.base import BaseCollector
from app.services.collectors.chatgpt_local import ChatGPTLocalMixin

# Mixins
from app.services.collectors.chatgpt_oauth import ChatGPTWebOAuthMixin
from app.services.collectors.chatgpt_web import ChatGPTWebMixin

logger = logging.getLogger(__name__)


class ChatGPTCollector(
    ChatGPTWebOAuthMixin,
    ChatGPTWebMixin,
    ChatGPTLocalMixin,
    BaseCollector,
):
    """
    Orchestrator for ChatGPT data collection.
    Inherits from mixins for auth, API, and local strategies.
    """

    PROVIDER_ID = "chatgpt"
    DEFAULT_WINDOW_TYPE = "weekly"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "web": ("Web Gateway (web)", "_strategy_web_wrap"),
        "cli": ("CLI RPC (local)", "_collect_via_cli_rpc"),
        "local": ("Local Enrichment (local)", "_strategy_local_enrichment", {"enrich": True}),
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
        from datetime import datetime

        self._primary_reset_at = None
        for card in primary:
            reset_at_str = card.get("reset_at")
            if not reset_at_str:
                continue
            try:
                dt = datetime.fromisoformat(reset_at_str.replace("Z", "+00:00"))
                self._primary_reset_at = dt
                break  # We only need one reset_at for ChatGPT
            except (ValueError, TypeError):
                continue

    async def is_configured(self) -> bool:
        """Check if ChatGPT auth data (logs or tokens) is present."""
        # Use None for client to avoid triggering background refreshes during config check
        auth = await self._get_auth_data(None)

        # Check if we have an OAuth token, a session cookie, or local logs
        has_auth = bool(auth.get("token"))
        has_local = auth.get("source") == "local"

        return has_auth or has_local

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for ChatGPT."""
        return [
            self._collect_via_cli_rpc,
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
