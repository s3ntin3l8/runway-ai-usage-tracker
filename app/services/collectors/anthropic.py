"""
Anthropic collector orchestrating OAuth, Web Scraping, and Local Log strategies.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.services.collectors.anthropic_local import AnthropicLocalMixin

# Mixins
from app.services.collectors.anthropic_oauth import AnthropicOAuthMixin
from app.services.collectors.anthropic_web import AnthropicWebMixin
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

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
    DEFAULT_WINDOW_TYPE = "weekly"  # Free tier; Pro/paid windows are tagged per-card

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
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
        self._last_statusline_data = {}  # Cache for hybrid fallback
        self._terminal_failure = False  # Guard for invalid_grant

    async def is_configured(self) -> bool:
        """Check if Anthropic credentials (OAuth, token, or local logs) are present."""
        if await self._get_current_token():
            return True
        if self._is_valid_credential(settings.CLAUDE_CODE_OAUTH_TOKEN):
            return True
        # Check for statusline/local logs
        if await self._strategy_statusline():
            return True
        return False

    async def _get_current_token(self) -> str | None:
        """Fetch current access token from sidecar cache or credentials file."""
        # 1. Check sidecar cache first (fastest, supports multi-account)
        cache_data = await token_cache.get_with_metadata(
            "anthropic", account_id=self.account_id
        )
        if cache_data:
            tokens, metadata = cache_data
            token = tokens.get("oauth_token")
            if token:
                # Store source for card labeling
                source = metadata.get("source") or "sidecar"
                self._current_input_source = (
                    "manual" if source == "manual_config" else "sidecar"
                )
                return token

        # 2. Fallback to reading the local credentials file
        if not self.account_id:
            creds = await self._get_credentials()
            if creds:
                oauth = creds.get("claudeAiOauth", {})
                token = oauth.get("accessToken")
                if token:
                    self._current_input_source = "server"
                    # Mirror into token cache so the Tokens health tab can see it
                    token_data: dict[str, str] = {"oauth_token": token}
                    if oauth.get("refreshToken"):
                        token_data["refresh_token"] = oauth["refreshToken"]
                    label = creds.get("oauthAccount", {}).get("emailAddress")
                    await token_cache.store(
                        "anthropic", token_data, account_id=None, account_label=label, source="server"
                    )
                return token
        return None

    async def _is_token_expired(self) -> bool:
        """Check if Claude token is expired."""
        try:
            # Check sidecar cache for expiration info
            token_data = await token_cache.get_all_tokens("anthropic", account_id=self.account_id)
            if token_data and "expires_at" in token_data:
                return datetime.now(UTC).timestamp() > token_data["expires_at"]

            # Fallback to credentials file
            creds = await self._get_credentials()
            if creds:
                expires_at = creds.get("claudeAiOauth", {}).get("expiresAt")
                if expires_at:
                    # Some formats use ms, some iso strings
                    if isinstance(expires_at, int | float):
                        if expires_at > 1e12:  # ms
                            expires_at /= 1000
                        return datetime.now(UTC).timestamp() > expires_at
                    if isinstance(expires_at, str):
                        exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                        return datetime.now(UTC) > exp_dt
            return False
        except Exception as e:
            logger.debug(f"Could not check Anthropic token expiration: {e}")
            return False

    def _is_error_result(self, results: list[dict[str, Any]]) -> bool:
        """Anthropic specific error check."""
        if not results:
            return True
        return all(r.get("remaining") == "ERR" for r in results)

    def _fallback_strategies(self) -> list[Any]:
        """Return ordered fallback strategies."""
        return [
            self._get_claude_via_web_api,
            self._strategy_cli_pty,
            self._strategy_local_enhanced,
        ]

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Hybrid primary strategy: Statusline merged with Web/OAuth."""
        results = []

        # 1. Fetch from Statusline (Fast Local)
        statusline_results = await self._strategy_statusline()
        if statusline_results:
            results.extend(statusline_results)

        # 2. Check if we have a Session Key (yields to Web API strategy)
        token = await self._get_current_token()
        if token and (token.startswith("sk-ant-sid") or "sessionKey=" in token):
            web_results = await self._get_claude_via_web_api(client)
            if web_results:
                seen_services = {r["service_name"] for r in web_results}
                results = [r for r in results if r["service_name"] not in seen_services]
                results.extend(web_results)
                return results

        # 3. Fallback to OAuth (Full API) if no session key or web strategy failed
        token = await self._get_valid_token(client)
        if token:
            oauth_results = await self._get_claude_oauth(client, token)
            if oauth_results:
                seen_services = {r["service_name"] for r in oauth_results}
                results = [r for r in results if r["service_name"] not in seen_services]
                results.extend(oauth_results)

        return results

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return final error card."""
        from app.core.utils import error_card

        return [
            error_card(
                "Claude (Anthropic)", "🟠", "No authentication found", error_type="missing_config"
            )
        ]

    async def reset(self):
        """Reset terminal failure and backoff state."""
        self._terminal_failure = False
        self._refresh_backoff_seconds = 30
        logger.info(f"Reset Anthropic collector state for account {self.account_id or 'default'}")
