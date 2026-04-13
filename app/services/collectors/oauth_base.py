import asyncio
import json
import logging
import os

import httpx

from app.core.config import settings
from app.core.utils import safe_write_json
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class OAuthBaseCollector(BaseCollector):
    """
    Base class for collectors that use OAuth with refresh tokens.
    Handles token expiration, locking, and persistence logic.
    Supports multi-account isolation.
    """

    def __init__(
        self,
        provider_name: str,
        credentials_path: str,
        account_id: str | None = None,
        account_label: str | None = None,
    ):
        super().__init__(account_id=account_id, account_label=account_label)
        self.provider_name = provider_name
        self._credentials_path = credentials_path
        self._token_lock = asyncio.Lock()

    async def _get_credentials(self) -> dict | None:
        """Load credentials from file or cache."""
        if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
            return None

        try:
            if await asyncio.to_thread(os.path.exists, self._credentials_path):

                def read_json(path):
                    with open(path) as f:
                        return json.load(f)

                return await asyncio.to_thread(read_json, self._credentials_path)
        except Exception as e:
            logger.warning(f"Could not load {self.provider_name} credentials: {e}")
        return None

    def _persist_credentials(self, creds: dict):
        """Persist refreshed credentials to file."""
        if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
            logger.info(
                f"Skipping {self.provider_name} token persistence (local credential scraping disabled)"
            )
            return

        try:
            safe_write_json(self._credentials_path, creds)
        except Exception as e:
            logger.error(f"Failed to persist {self.provider_name} credentials: {e}")

    async def _get_valid_token(self, client: httpx.AsyncClient) -> str | None:
        """Get a valid token, refreshing if necessary."""
        async with self._token_lock:
            # 1. Check if we have a valid token in the core cache first (Sidecar provided or recently refreshed)
            token = await self._get_current_token()
            if token and not await self._is_token_expired():
                return token

            # 2. Check if we can refresh
            logger.info(
                f"Refreshing {self.provider_name} access token for account {self.account_id or 'default'}..."
            )
            new_creds = await self._execute_refresh(client)
            if new_creds:
                self._persist_credentials(new_creds)
                return new_creds.get("access_token")

            return None

    async def _store_sidecar_token(
        self, provider: str, access_token: str, refresh_token: str | None = None
    ):
        """Update sidecar token cache with newly refreshed tokens."""
        data = {"oauth_token": access_token}
        if refresh_token:
            data["refresh_token"] = refresh_token

        await token_cache.store(provider, data, account_id=self.account_id)

    # These must be implemented by subclasses
    async def _get_current_token(self) -> str | None:
        raise NotImplementedError

    async def _is_token_expired(self) -> bool:
        raise NotImplementedError

    async def _execute_refresh(self, client: httpx.AsyncClient) -> dict | None:
        raise NotImplementedError
