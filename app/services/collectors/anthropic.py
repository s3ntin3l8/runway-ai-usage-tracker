"""
Anthropic collector orchestrating OAuth, Web Scraping, and Local Log strategies.
"""

import os
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import httpx

from app.core.config import settings
from app.services.collectors.base import BaseCollector
from app.services.collectors.oauth_base import OAuthBaseCollector
from app.services.token_cache import token_cache
from app.services.credential_provider import credential_provider

# Mixins
from app.services.collectors.anthropic_oauth import AnthropicOAuthMixin
from app.services.collectors.anthropic_web import AnthropicWebMixin
from app.services.collectors.anthropic_local import AnthropicLocalMixin

logger = logging.getLogger(__name__)


class AnthropicCollector(
    AnthropicOAuthMixin,
    AnthropicWebMixin,
    AnthropicLocalMixin,
):
    """
    Orchestrator for Anthropic data collection.
    Inherits from OAuthBaseCollector for core token logic and mixins for strategies.
    """

    PROVIDER_ID = "anthropic"
    DEFAULT_WINDOW_TYPE = "rolling"  # Free tier; Pro/paid windows are tagged per-card

    def __init__(self, account_id: Optional[str] = None, account_label: Optional[str] = None):
        """Initialize orchestrator."""
        # Find credentials via centralized provider
        credentials_path = credential_provider.get_anthropic_credentials_path()
        if not credentials_path:
            credentials_path = settings.ANTHROPIC_OAUTH_PATH

        super().__init__(
            provider_name="Anthropic",
            credentials_path=credentials_path,
            account_id=account_id,
            account_label=account_label,
        )

        # Result caching for API calls
        self._cached_api_results = None
        self._last_api_fetch = None
        
        self._refresh_backoff_seconds = 30
        self._max_refresh_backoff = 21600  # 6 hours max
        self._last_statusline_data = {}    # Cache for hybrid fallback
        self._terminal_failure = False     # Guard for invalid_grant

    async def _get_current_token(self) -> Optional[str]:
        """Fetch current access token from sidecar cache or credentials file."""
        # 1. Check sidecar cache first (fastest, supports multi-account)
        token = await token_cache.get_token(
            "anthropic", "oauth_token", account_id=self.account_id
        )
        if token:
            return token

        # 2. Fallback to reading the local credentials file
        if not self.account_id:
            creds = await self._get_credentials()
            if creds:
                return creds.get("claudeAiOauth", {}).get("accessToken")
        return None

    def _is_error_result(self, results: List[Dict[str, Any]]) -> bool:
        """Anthropic specific error check."""
        if not results:
            return True
        return all(r.get("remaining") == "ERR" for r in results)

    def _fallback_strategies(self) -> List[Any]:
        """Return ordered fallback strategies."""
        return [
            self._get_claude_via_web_api,
            self._strategy_cli_pty,
            self._strategy_local_enhanced,
        ]

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Hybrid primary strategy: Statusline merged with OAuth."""
        results = []
        
        # 1. Fetch from Statusline (Fast Local)
        statusline_results = await self._strategy_statusline()
        if statusline_results:
            results.extend(statusline_results)
            
        # 2. Fetch from OAuth (Full API)
        token = await self._get_valid_token(client)
        if token:
            oauth_results = await self._get_claude_oauth_with_cache(client, token)
            if oauth_results:
                # Simple deduplication: Prefer OAuth over Statusline for same window
                seen_services = {r["service_name"] for r in oauth_results}
                results = [r for r in results if r["service_name"] not in seen_services]
                results.extend(oauth_results)

        return results

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return final error card."""
        from app.core.utils import error_card
        return [error_card("Claude (Anthropic)", "🟠", "No authentication found", error_type="missing_config")]

    async def reset(self):
        """Reset terminal failure and backoff state."""
        self._terminal_failure = False
        self._refresh_backoff_seconds = 30
        logger.info(f"Reset Anthropic collector state for account {self.account_id or 'default'}")
