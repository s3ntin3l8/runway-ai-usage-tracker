import logging
import time

import httpx

from app.core.config import settings
from app.core.utils import IdentityExtractor, http_request_with_retry
from app.services.collectors.oauth_base import OAuthBaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class GeminiOAuthMixin(OAuthBaseCollector):
    """Mixin for Gemini OAuth token management."""

    async def _get_current_token(self) -> str | None:
        """Get the current access token.

        Priority rules:
        - Local-mode collectors (account_id is None): try the local credentials
          file first so the correct Google account is always used. Mirror into
          the cache afterward so the token health tab can display it. Only fall
          back to the cache if the local file provides no token.
        - Sidecar-mode collectors (account_id is set): keep the existing cache-first
          behaviour; there is no local file to consult.
        """
        if self.account_id:
            # Sidecar mode: the cache is the only source of truth.
            return await token_cache.get_token("gemini", "oauth_token", account_id=self.account_id)

        # Local mode: prefer the local credentials file to avoid picking up a
        # sidecar token that belongs to a different Google account.
        creds = await self._get_credentials()
        if creds:
            token = creds.get("access_token")
            if token:
                # Mirror into token cache so the Tokens health tab can see it.
                token_data: dict[str, str] = {"oauth_token": token}
                if creds.get("refresh_token"):
                    token_data["refresh_token"] = creds["refresh_token"]
                await token_cache.store("gemini", token_data, account_id=None, account_label=None)
                return token

        # Credentials file absent or scraping disabled — fall back to cache.
        # Note: if the only cached entry belongs to a sidecar this can still
        # return the wrong token, but that is pre-existing behaviour and only
        # occurs when local credential scraping is entirely unavailable.
        return await token_cache.get_token("gemini", "oauth_token", account_id=None)

    async def _is_token_expired(self) -> bool:
        """Check if Gemini token is expired.

        For sidecar-mode collectors (account_id set), there is no local
        credentials file to derive expiry from. The token cache already enforces
        a 30-minute TTL, so we treat sidecar tokens as always valid here and
        avoid triggering a spurious refresh that cannot succeed without a local
        refresh_token.
        """
        if self.account_id:
            # Sidecar mode: defer freshness enforcement to the cache TTL.
            return False

        try:
            creds = await self._get_credentials()
            if creds:
                expiry_ms = creds.get("expiry_date")
                if expiry_ms:  # Missing or zero → no expiry info, assume still valid
                    return expiry_ms < (time.time() * 1000)
                return False
        except Exception as e:
            logger.debug(f"Could not check Gemini token expiration: {e}")
        return True

    async def _execute_refresh(self, client: httpx.AsyncClient) -> dict | None:
        """Execute the HTTP request to refresh the token for Gemini."""
        creds = await self._get_credentials()
        if not creds:
            return None

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("No refresh token in Gemini credentials")
            return None

        # Auto-discover client_id from explicit field or id_token if not in settings
        client_id = settings.GEMINI_OAUTH_CLIENT_ID
        if not client_id:
            client_id = creds.get("client_id") or creds.get("clientId")

        if not client_id and "id_token" in creds:
            token_client_id = IdentityExtractor.get_client_id_from_jwt(creds["id_token"])
            if token_client_id:
                client_id = token_client_id
                logger.info(f"Auto-discovered Gemini Client ID: {client_id[:10]}...")

        if not client_id:
            logger.warning("Gemini Client ID missing (set GEMINI_OAUTH_CLIENT_ID)")
            return None

        try:
            resp = await http_request_with_retry(
                client,
                "POST",
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=10,
            )

            if resp.status_code == 200:
                new_data = resp.json()
                creds["access_token"] = new_data["access_token"]
                # Expiry is in seconds in response, convert to ms
                creds["expiry_date"] = int(time.time() * 1000) + (new_data["expires_in"] * 1000)

                # Update sidecar cache
                await self._store_sidecar_token("gemini", new_data["access_token"])

                return creds
            logger.warning(
                f"Gemini token refresh failed with status {resp.status_code}: {resp.text[:100]}"
            )
            return None
        except Exception as e:
            logger.error(f"Failed to refresh Gemini token: {e}")
            return None
