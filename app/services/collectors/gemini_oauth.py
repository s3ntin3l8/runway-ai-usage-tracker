import logging
import os
import time
from typing import Optional, Dict
import httpx
from datetime import datetime, timezone
from app.core.config import settings
from app.core.utils import IdentityExtractor, http_request_with_retry
from app.services.token_cache import token_cache
from app.services.collectors.oauth_base import OAuthBaseCollector

logger = logging.getLogger(__name__)

class GeminiOAuthMixin(OAuthBaseCollector):
    """Mixin for Gemini OAuth token management."""
    
    async def _get_current_token(self) -> Optional[str]:
        """Get the current access token."""
        # Check sidecar cache first
        token = await token_cache.get_token(
            "gemini", "oauth_token", account_id=self.account_id
        )
        if not token and not self.account_id:
            creds = await self._get_credentials()
            if creds:
                token = creds.get("access_token")
        return token

    async def _is_token_expired(self) -> bool:
        """Check if Gemini token is expired."""
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

    async def _execute_refresh(self, client: httpx.AsyncClient) -> Optional[Dict]:
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
                creds["expiry_date"] = int(time.time() * 1000) + (
                    new_data["expires_in"] * 1000
                )

                # Update sidecar cache
                await self._store_sidecar_token("gemini", new_data["access_token"])

                return creds
            else:
                logger.warning(
                    f"Gemini token refresh failed with status {resp.status_code}: {resp.text[:100]}"
                )
                return None
        except Exception as e:
            logger.error(f"Failed to refresh Gemini token: {e}")
            return None
