import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.browser_cookies import get_chatgpt_device_id, get_chatgpt_session_token
from app.core.config import is_local_credential_scraping_enabled, settings
from app.core.utils import http_request_with_retry, safe_write_json
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class ChatGPTAuthMixin:
    """Mixin for ChatGPT authentication and token management."""

    async def _get_auth_data(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """
        Retrieve ChatGPT auth with priority: OAUTH -> Browser Cookies -> Sidecar Cache.
        """
        # Priority 1 & 2: Env var or auth.json (Centralized in CredentialProvider)
        auth_data = credential_provider.get_chatgpt_data()
        token = auth_data.get("access_token")
        account_id = auth_data.get("account_id")
        refresh_token = auth_data.get("refresh_token")

        if token:
            # Check if we need to refresh the OAuth token (if it's from auth.json and stale)
            last_refresh = auth_data.get("last_refresh")
            if last_refresh and refresh_token:
                try:
                    lr_dt = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
                    if (datetime.now(UTC) - lr_dt).days >= 8:
                        logger.info("ChatGPT OAuth token is stale (8+ days), refreshing...")
                        new_tokens = await self._refresh_oauth_token(client, refresh_token)
                        if new_tokens:
                            token = new_tokens["access_token"]
                except Exception as e:
                    logger.debug(f"Failed to check/refresh stale ChatGPT token: {e}")

            return {
                "token": token,
                "account_id": account_id,
                "refresh_token": refresh_token,
                "source": "credential_provider",
            }

        # Priority 3: Browser Cookies
        session_token = await asyncio.to_thread(get_chatgpt_session_token)
        if not session_token:
            # Check sidecar cache for cookie
            session_token = await token_cache.get_token(
                "chatgpt",
                "cookie___Secure-next-auth.session-token",
                account_id=self.account_id,
            )

        if session_token:
            # Try to get refreshed token from in-memory cache
            now = datetime.now(UTC)
            if (
                getattr(self, "_refreshed_token", None)
                and getattr(self, "_refreshed_token_expiry", None)
                and now < self._refreshed_token_expiry
            ):
                return {"token": self._refreshed_token, "source": "cookies_cached"}

            # Refresh Bearer token using session cookie
            refreshed = await self._refresh_access_token(client, session_token)
            if refreshed:
                self._refreshed_token = refreshed
                self._refreshed_token_expiry = now + timedelta(hours=1)
                return {"token": refreshed, "source": "cookies"}

        # Priority 4: Sidecar cache (direct OAuth token)
        token = await token_cache.get_token("chatgpt", "oauth_token", account_id=self.account_id)
        if token:
            logger.debug("Using OAuth token from sidecar cache")
            return {"token": token, "source": "sidecar_cache"}

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
            )
            if resp.status_code == 200:
                data = resp.json()
                new_data = {
                    "access_token": data.get("access_token"),
                    "refresh_token": data.get("refresh_token"),
                    "id_token": data.get("id_token"),
                }
                # Persist to disk if possible
                await self._save_refreshed_oauth_token(new_data)
                return new_data
        except Exception as e:
            logger.debug(f"Error refreshing ChatGPT OAuth token: {e}")
        return None

    async def _save_refreshed_oauth_token(self, data: dict[str, str]):
        """Persist refreshed OAuth tokens back to auth.json."""
        if not is_local_credential_scraping_enabled():
            return

        auth_path = settings.CHATGPT_AUTH_PATH
        if not os.path.exists(auth_path):
            return

        try:
            # Read existing
            with open(auth_path) as f:
                existing = json.load(f)

            # Update
            existing["tokens"]["access_token"] = data["access_token"]
            if data.get("refresh_token"):
                existing["refresh_token"] = data["refresh_token"]
            if data.get("id_token"):
                existing["id_token"] = data["id_token"]
            existing["last_refresh"] = datetime.now(UTC).isoformat()

            # Write back
            safe_write_json(auth_path, existing)
            logger.info(f"Updated ChatGPT OAuth tokens in {auth_path}")
        except Exception as e:
            logger.debug(f"Failed to persist refreshed ChatGPT token: {e}")

    async def _get_device_id(self) -> str:
        """Get device ID from cookies or use generated session ID."""
        cookie_id = await asyncio.to_thread(get_chatgpt_device_id)
        if cookie_id:
            return cookie_id

        # Fallback to generated ID (cached on instance)
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
