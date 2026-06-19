import logging
import time

import httpx

from app.core.date_utils import parse_iso8601_utc
from app.services.collectors.oauth_base import OAuthBaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class AntigravityOAuthMixin(OAuthBaseCollector):
    """OAuth token management for the Antigravity CLI.

    The agy token file uses a nested structure:
    ``{"auth_method":"consumer","token":{"access_token":…,"refresh_token":…,"expiry":"…"}}``

    agy refreshes its own token in the background on each CLI invocation; Runway
    reads the on-disk file every poll to pick up whatever agy last wrote.  We
    cannot refresh the token independently (no OAuth client_id in the file), so
    if the file token is expired we wait for agy to run again.
    """

    async def _get_token_data(self) -> dict:
        """Return the ``token`` sub-dict from the agy credentials file."""
        raw = await self._get_credentials()
        return (raw or {}).get("token", {})

    async def _get_current_token(self) -> str | None:
        """Return the current access token.

        - Sidecar mode (account_id set): pull from cache.
        - Local mode: read the on-disk credentials file; mirror into cache so
          the Token Health tab can display it.
        """
        if self.account_id:
            cache_data = await token_cache.get_with_metadata(
                "antigravity", account_id=self.account_id
            )
            if not cache_data:
                # Identity-mismatch fallback: the agy token file carries no id_token,
                # so the sidecar-pushed token is cached under a refresh-token-derived
                # hash — NOT the email that seeds self.account_id from LatestUsage, and
                # antigravity has no "default" cache entry to catch the miss. Fall back
                # to the newest cached entry (single Google account → it is the right
                # token). Keep self.account_id as the email; do not adopt the hash.
                cache_data = await token_cache.get_with_metadata("antigravity", account_id=None)
            if cache_data:
                tokens, metadata = cache_data
                source = metadata.get("source") or "sidecar"
                self._current_input_source = (
                    "config" if source in ("config", "manual_config") else "sidecar"
                )
                cached_label = metadata.get("account_label")
                if cached_label and (not self.account_label or self.account_label == "Default"):
                    self.account_label = cached_label
                return tokens.get("oauth_token")
            return None

        # Local mode: prefer the on-disk file.
        td = await self._get_token_data()
        token = td.get("access_token")
        if token:
            self._current_input_source = "server"
            token_store: dict[str, str] = {"oauth_token": token}
            if td.get("refresh_token"):
                token_store["refresh_token"] = td["refresh_token"]
            expiry = td.get("expiry")
            if expiry:
                try:
                    expiry_dt = parse_iso8601_utc(expiry)
                    expiry_ms = int(expiry_dt.timestamp() * 1000)
                    token_store["expiry_date"] = str(expiry_ms)
                except (ValueError, TypeError):
                    pass

            await token_cache.store(
                "antigravity",
                token_store,
                account_id=None,
                account_label=self.account_label or None,
                source="server",
            )
            return token

        # Fall back to cache (multi-host: sidecar shipped the token).
        cache_data = await token_cache.get_with_metadata("antigravity", account_id=None)
        if cache_data:
            tokens, metadata = cache_data
            source = metadata.get("source") or "sidecar"
            self._current_input_source = (
                "config" if source in ("config", "manual_config") else "sidecar"
            )
            cached_label = metadata.get("account_label")
            if cached_label and (not self.account_label or self.account_label == "Default"):
                self.account_label = cached_label
            return tokens.get("oauth_token")
        return None

    async def _is_token_expired(self) -> bool:
        """Check expiry from the ``token.expiry`` ISO 8601 field."""
        if self.account_id:
            return False  # Sidecar mode: rely on cache TTL.
        try:
            td = await self._get_token_data()
            expiry_str = td.get("expiry")
            if expiry_str:
                expiry_dt = parse_iso8601_utc(expiry_str)
                return expiry_dt.timestamp() < time.time()
        except Exception as e:
            logger.debug("Could not check Antigravity token expiry: %s", e)
        return False  # Unknown → assume valid; agy manages its own refresh.

    async def _execute_refresh(self, client: httpx.AsyncClient) -> dict | None:
        # agy owns the token refresh cycle; Runway cannot refresh independently
        # because the OAuth client_id is not stored in the token file.
        return None
