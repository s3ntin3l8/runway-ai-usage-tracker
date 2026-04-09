import os
import json
import logging
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any
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
    """

    def __init__(self, provider_name: str, credentials_path: str):
        self.provider_name = provider_name
        self._credentials_path = credentials_path
        self._refresh_lock = asyncio.Lock()

        # Backoff and failure tracking
        self._last_refresh_failure = None
        self._refresh_backoff_seconds = 300  # 5 minutes default
        self._terminal_failure = False

    async def _get_credentials(self) -> Optional[Dict]:
        """Load credentials from file or cache."""
        if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
            return None

        try:
            if await asyncio.to_thread(os.path.exists, self._credentials_path):

                def read_json(path):
                    with open(path, "r") as f:
                        return json.load(f)

                return await asyncio.to_thread(read_json, self._credentials_path)
        except Exception as e:
            logger.warning(f"Could not load {self.provider_name} credentials: {e}")
        return None

    def _persist_credentials(self, creds: Dict):
        """Persist refreshed credentials to file."""
        if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
            logger.info(
                f"Skipping {self.provider_name} token persistence (local credential scraping disabled)"
            )
            return

        try:
            safe_write_json(self._credentials_path, creds)
            logger.info(
                f"Persisted {self.provider_name} refreshed tokens to {self._credentials_path}"
            )
        except Exception as e:
            logger.error(f"Failed to persist {self.provider_name} tokens: {e}")

    def _can_attempt_refresh(self) -> bool:
        """Check if we should attempt token refresh based on backoff."""
        if self._terminal_failure:
            return False

        if self._last_refresh_failure:
            elapsed = (
                datetime.now(timezone.utc) - self._last_refresh_failure
            ).total_seconds()
            if elapsed < self._refresh_backoff_seconds:
                return False
        return True

    async def _get_valid_token(self, client: httpx.AsyncClient) -> Optional[str]:
        """
        Get a valid access token, refreshing if necessary.
        Uses locking to ensure only one refresh happens at a time.
        """
        # Get current token from whichever source the subclass uses
        token = await self._get_current_token()

        if token and not await self._is_token_expired():
            return token

        async with self._refresh_lock:
            # Re-check under lock
            token = await self._get_current_token()
            if token and not await self._is_token_expired():
                return token

            if not self._can_attempt_refresh():
                logger.debug(
                    f"{self.provider_name} token refresh backed off or terminal failure"
                )
                return None

            logger.info(f"Attempting {self.provider_name} OAuth token refresh")
            new_creds = await self._execute_refresh(client)

            if new_creds:
                self._persist_credentials(new_creds)
                self._last_refresh_failure = None
                return new_creds.get("access_token")
            else:
                self._last_refresh_failure = datetime.now(timezone.utc)
                return None

    # Methods to be implemented or customized by subclasses
    async def _get_current_token(self) -> Optional[str]:
        """Get the current access token."""
        raise NotImplementedError

    async def _is_token_expired(self) -> bool:
        """Check if the current token is expired."""
        raise NotImplementedError

    async def _execute_refresh(self, client: httpx.AsyncClient) -> Optional[Dict]:
        """Execute the HTTP request to refresh the token."""
        raise NotImplementedError
