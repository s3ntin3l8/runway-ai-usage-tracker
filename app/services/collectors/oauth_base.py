import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta

import httpx

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

    # Rate-limit backoff for the OAuth token endpoint itself
    RATE_LIMIT_BASE_BACKOFF_SECONDS = 300  # 5 minutes
    RATE_LIMIT_MAX_BACKOFF_SECONDS = 21600  # 6 hours
    MAX_RATE_LIMIT_FAILURES = 5
    TOKEN_REFRESH_THRESHOLD_SECONDS = 600  # 10 minutes proactive refresh

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
        self._refresh_429_fail_count = 0
        self._last_refresh_429_backoff_until: datetime | None = None

    async def _get_credentials(self) -> dict | None:
        """Load credentials from file or cache."""
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
        try:
            safe_write_json(self._credentials_path, creds)
        except Exception as e:
            logger.error(f"Failed to persist {self.provider_name} credentials: {e}")

    # ── Token-endpoint rate-limit backoff (mirrors onWatch behaviour) ──────────

    def _is_refresh_backoff_active(self) -> bool:
        """Check if the OAuth token endpoint is currently in rate-limit backoff."""
        if self._last_refresh_429_backoff_until is None:
            return False
        return datetime.now(UTC) < self._last_refresh_429_backoff_until

    def _set_refresh_429_backoff(self, retry_after: float | None = None) -> None:
        """Set exponential backoff after a 429 from the OAuth token endpoint."""
        self._refresh_429_fail_count += 1

        if retry_after and retry_after > 0:
            wait_sec = retry_after
        else:
            shift = min(self._refresh_429_fail_count - 1, 10)  # prevent overflow
            wait_sec = self.RATE_LIMIT_BASE_BACKOFF_SECONDS * (1 << shift)
            wait_sec = min(wait_sec, self.RATE_LIMIT_MAX_BACKOFF_SECONDS)

        self._last_refresh_429_backoff_until = datetime.now(UTC) + timedelta(seconds=wait_sec)
        logger.warning(
            f"{self.provider_name} OAuth token endpoint rate limited. "
            f"Backoff #{self._refresh_429_fail_count}: {wait_sec:.0f}s "
            f"(resume at {self._last_refresh_429_backoff_until.isoformat()})"
        )

    def _clear_refresh_429_backoff(self) -> None:
        """Clear rate-limit backoff state after a successful refresh."""
        if self._refresh_429_fail_count > 0:
            logger.debug(f"Cleared {self.provider_name} OAuth token endpoint backoff")
        self._refresh_429_fail_count = 0
        self._last_refresh_429_backoff_until = None

    async def _is_token_expiring_soon(self) -> bool:
        """Check if token expires within the proactive refresh threshold.

        Subclasses may override to implement provider-specific expiry checks.
        Default returns False (conservative — no pre-emptive refresh).
        """
        return False

    async def _get_valid_token(
        self, client: httpx.AsyncClient, force_refresh: bool = False
    ) -> str | None:
        """Get a valid token, refreshing if necessary.

        Respects token-endpoint rate-limit backoff and performs proactive
        refresh when the token is expiring within the threshold window.
        """
        async with self._token_lock:
            # 1. Check if the token endpoint itself is in backoff
            if self._is_refresh_backoff_active():
                logger.debug(f"Skipping {self.provider_name} token refresh — in rate-limit backoff")
                token = await self._get_current_token()
                if token and not await self._is_token_expired():
                    return token
                return None

            # 2. Check if we have a valid token
            token = await self._get_current_token()
            if token and not await self._is_token_expired() and not force_refresh:
                # Pre-emptive refresh: if the token is expiring soon, refresh
                # proactively to avoid an expiry race during the next API call.
                if await self._is_token_expiring_soon():
                    logger.info(
                        f"{self.provider_name} token expiring soon, refreshing proactively..."
                    )
                    force_refresh = True
                else:
                    return token

            if token and not force_refresh:
                return token

            # 3. Attempt refresh
            logger.info(
                f"Refreshing {self.provider_name} access token for account {self.account_id or 'default'}..."
            )
            new_creds = await self._execute_refresh(client)
            if new_creds:
                self._persist_credentials(new_creds)
                access = new_creds.get("access_token")
                if access:
                    # Update cache so token health stays current after refresh
                    await self._store_sidecar_token(
                        self.provider_name,
                        access,
                        new_creds.get("refresh_token"),
                        new_creds.get("expiry_date"),
                    )
                self._clear_refresh_429_backoff()
                return access

            # Refresh failed — return current token if still valid so the caller
            # can attempt the API call with the existing token rather than failing
            # entirely because the refresh endpoint is struggling.
            if token and not await self._is_token_expired():
                return token
            return None

    async def _store_sidecar_token(
        self,
        provider: str,
        access_token: str,
        refresh_token: str | None = None,
        expiry_date: str | int | None = None,
    ):
        """Update sidecar token cache with newly refreshed tokens."""
        data = {"oauth_token": access_token}
        if refresh_token:
            data["refresh_token"] = refresh_token
        # Carry the new access-token expiry so the cache freshness guard knows this
        # refreshed token outranks a staler sidecar push (opaque tokens have no exp).
        if expiry_date is not None:
            data["expiry_date"] = str(expiry_date)

        await token_cache.store(provider, data, account_id=self.account_id)

    # These must be implemented by subclasses
    async def _get_current_token(self) -> str | None:
        raise NotImplementedError

    async def _is_token_expired(self) -> bool:
        raise NotImplementedError

    async def _execute_refresh(self, client: httpx.AsyncClient) -> dict | None:
        raise NotImplementedError
