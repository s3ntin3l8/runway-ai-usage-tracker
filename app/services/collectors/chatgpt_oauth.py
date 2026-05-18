import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import http_request_with_retry
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class ChatGPTWebOAuthMixin:
    """Mixin for ChatGPT authentication and token management."""

    async def _get_auth_data(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """
        Retrieve ChatGPT auth with priority: Instance Cache -> OAUTH -> Global Cache -> Browser Cookies.
        """
        # Priority 0: Instance-level Refreshed Token (short-lived in-memory cache)
        # This is populated after a successful cookie-based refresh.
        now = datetime.now(UTC)
        if (
            getattr(self, "_refreshed_token", None)
            and getattr(self, "_refreshed_token_expiry", None)
            and now < self._refreshed_token_expiry
        ):
            self._current_input_source = getattr(self, "_refreshed_input_source", "unknown")
            return {
                "token": self._refreshed_token,
                "source": self.DATA_SOURCE_WEB,
                "input_source": self._current_input_source,
            }

        # Priority 1 & 2: Env var or auth.json (Centralized in CredentialProvider)
        auth_data = credential_provider.get_chatgpt_data()
        token = auth_data.get("access_token")
        account_id = auth_data.get("account_id")
        refresh_token = auth_data.get("refresh_token")

        if token:
            # Check if we need to refresh the OAuth token (if it's from auth.json and stale)
            last_refresh = auth_data.get("last_refresh")
            if client and last_refresh and refresh_token:
                try:
                    lr_dt = parse_iso8601_utc(last_refresh)
                    if (datetime.now(UTC) - lr_dt).days >= 8:
                        logger.info("ChatGPT OAuth token is stale (8+ days), refreshing...")
                        new_tokens = await self._refresh_oauth_token(client, refresh_token)
                        if new_tokens:
                            token = new_tokens["access_token"]
                except Exception as e:
                    logger.debug(f"Failed to check/refresh stale ChatGPT token: {e}")

            input_source = getattr(auth_data, "sources", {}).get("access_token", "server")
            self._current_input_source = input_source
            return {
                "token": token,
                "account_id": account_id,
                "refresh_token": refresh_token,
                "source": self.DATA_SOURCE_API,
                "input_source": input_source,
            }

        # Priority 3: Direct OAuth token (Manual Config / Sidecar from global cache)
        cache_data = await token_cache.get_with_metadata("chatgpt", account_id=self.account_id)
        if cache_data:
            tokens, metadata = cache_data
            if tokens.get("oauth_token"):
                source = metadata.get("source") or "sidecar"
                input_source = "config" if source in ("config", "manual_config") else "sidecar"

                logger.debug(f"Using OAuth token from cache (input_source: {input_source})")
                return {
                    "token": tokens["oauth_token"],
                    "account_id": tokens.get("account_id"),
                    "source": self.DATA_SOURCE_API,
                    "input_source": input_source,
                }

        # Priority 4: Browser Cookies (local or forwarded)
        for c_key in (
            "session_cookie",
            "cookie_session",
            "cookie_sessionKey",
            "cookie___Secure-next-auth.session-token",
        ):
            token_metadata = await token_cache.get_with_metadata(
                "chatgpt", account_id=self.account_id
            )
            session_token = None
            input_source = "unknown"

            if token_metadata:
                tokens, metadata = token_metadata
                session_token = tokens.get(c_key)
                if session_token:
                    source_meta = metadata.get("source") or "sidecar"
                    input_source = (
                        "config" if source_meta in ("config", "manual_config") else "sidecar"
                    )

            if session_token:
                self._current_input_source = input_source
                if client:
                    # Refresh Bearer token using session cookie
                    refreshed = await self._refresh_access_token(client, session_token)
                    if refreshed:
                        self._refreshed_token = refreshed
                        self._refreshed_token_expiry = now + timedelta(hours=1)
                        self._refreshed_input_source = input_source
                        # Store in token cache for token health visibility
                        await token_cache.store(
                            "chatgpt",
                            {"oauth_token": refreshed},
                            account_id=self.account_id,
                            source=input_source if input_source == "config" else "server",
                        )
                        return {
                            "token": refreshed,
                            "source": self.DATA_SOURCE_WEB,
                            "input_source": input_source,
                        }
                else:
                    # When client is None (e.g. is_configured), just report that we have a session token
                    return {
                        "token": "present_via_session_cookie",
                        "source": self.DATA_SOURCE_WEB,
                        "input_source": input_source,
                    }

        return {}

    async def _refresh_oauth_token(
        self, client: httpx.AsyncClient, refresh_token: str
    ) -> dict[str, str] | None:
        """Refresh OAuth token using the OpenAI auth endpoint."""
        try:
            resp = await http_request_with_retry(
                client,
                "POST",
                "https://auth.openai.com/oauth/token",
                json={
                    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "openid profile email",
                },
                timeout=10,
                retry_on_429=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                new_data = {
                    "access_token": data.get("access_token"),
                    "refresh_token": data.get("refresh_token"),
                    "id_token": data.get("id_token"),
                }
                # Refreshed token is cached via token_cache.store() in the caller;
                # disk persistence of auth.json is the sidecar's responsibility now.
                return new_data
        except Exception as e:
            logger.debug(f"Error refreshing ChatGPT OAuth token: {e}")
        return None

    async def _get_device_id(self) -> str:
        """Get a device ID for ChatGPT API calls.

        Browser-cookie extraction moved to the sidecar; the server now generates
        a stable UUID per collector instance.
        """
        if not hasattr(self, "_device_id") or not self._device_id:
            self._device_id = str(uuid.uuid4())
        return self._device_id

    async def _refresh_access_token(
        self, client: httpx.AsyncClient, session_token: str
    ) -> str | None:
        """Exchange session cookie for a Bearer accessToken."""
        try:
            url = "https://chatgpt.com/api/auth/session"
            headers = {
                "Cookie": f"__Secure-next-auth.session-token={session_token}",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://chatgpt.com/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "oai-device-id": await self._get_device_id(),
                "oai-language": "en-US",
                "Priority": "u=1, i",
            }
            resp = await http_request_with_retry(client, "GET", url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("accessToken")
            logger.debug(f"Failed to refresh ChatGPT token: HTTP {resp.status_code}")
        except Exception as e:
            logger.debug(f"Error refreshing ChatGPT token: {e}")
        return None
