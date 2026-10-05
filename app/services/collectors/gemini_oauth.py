import logging
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import IdentityExtractor, http_request_with_retry
from app.services.collectors.oauth_base import OAuthBaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class GeminiOAuthMixin(OAuthBaseCollector):
    """Mixin for Gemini OAuth token management.

    The token cache is the only source: a sidecar-pushed bundle (the sidecar reads
    ``~/.gemini/oauth_creds.json``), an env var or a Settings-pasted credential. The server
    never reads the Gemini CLI's login file. Gemini does not rotate refresh tokens, so
    the server may refresh a pushed bundle itself (``ROTATING_REFRESH_PROVIDERS``).
    """

    async def _cached_gemini(self) -> tuple[dict[str, str], dict[str, Any]] | None:
        return await token_cache.get_with_metadata("gemini", account_id=self.account_id)

    async def _get_current_token(self) -> str | None:
        """Get the current access token from the token cache."""
        cache_data = await self._cached_gemini()
        if not cache_data:
            return None
        tokens, metadata = cache_data
        source = metadata.get("source") or "sidecar"
        self._current_input_source = (
            "config" if source in ("config", "manual_config") else "sidecar"
        )
        # Inherit account identity from cache metadata (extracted from the
        # sidecar-shipped id_token) so emitted cards carry the correct
        # email and resolve to the canonical account_id instead of
        # falling back to "default".
        cached_label = metadata.get("account_label")
        if cached_label and (not self.account_label or self.account_label == "Default"):
            self.account_label = cached_label
        return tokens.get("oauth_token")

    async def _is_token_expired(self) -> bool:
        """Whether the cached Gemini access token is past its known expiry.

        Expiry comes from the cached ``expiry_date`` (epoch ms) or the token's own ``exp``
        (``IdentityExtractor.exp_from_tokens``, without the id_token fallback). No expiry signal means "assume valid":
        absence of a signal is not evidence of expiry, and the cache TTL still applies.
        """
        cache_data = await self._cached_gemini()
        if not cache_data:
            return False
        # A refresh returns no fresh id_token, so its exp can be permanently stale: ignore it.
        tokens = {k: v for k, v in cache_data[0].items() if k != "id_token"}
        exp = IdentityExtractor.exp_from_tokens(tokens)
        return exp is not None and exp < time.time()

    async def _execute_refresh(self, client: httpx.AsyncClient) -> dict | None:
        """Refresh the cached Gemini bundle's access token.

        The new tokens are returned; ``_get_valid_token`` writes them back to the cache
        and any pinned source bundle via ``_store_sidecar_token``.
        """
        cache_data = await self._cached_gemini()
        if not cache_data:
            return None
        creds = cache_data[0]

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("No refresh token in cached Gemini credentials")
            return None

        # Auto-discover client_id: env var → bundle → id_token aud claim →
        # well-known Gemini CLI client_id (last-resort, matches CLI defaults).
        from app.services.token_refresher import _GEMINI_CLI_CLIENT_SECRET, _PROVIDER_CLIENT_IDS

        client_id = settings.GEMINI_OAUTH_CLIENT_ID
        if not client_id:
            client_id = creds.get("client_id") or creds.get("clientId")

        if not client_id and creds.get("id_token"):
            token_client_id = IdentityExtractor.get_client_id_from_jwt(creds["id_token"])
            if token_client_id:
                client_id = token_client_id
                logger.info(f"Auto-discovered Gemini Client ID: {client_id[:10]}...")

        if not client_id:
            client_id = _PROVIDER_CLIENT_IDS.get("gemini")

        if not client_id:
            logger.warning("Gemini Client ID missing (set GEMINI_OAUTH_CLIENT_ID)")
            return None

        client_secret = (
            creds.get("client_secret")
            or settings.GEMINI_OAUTH_CLIENT_SECRET
            or _GEMINI_CLI_CLIENT_SECRET
        )

        try:
            resp = await http_request_with_retry(
                client,
                "POST",
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=10,
                retry_on_429=False,
            )

            if resp.status_code == 200:
                new_data = resp.json()
                # Expiry is in seconds in the response; the cache carries epoch ms.
                return {
                    "access_token": new_data["access_token"],
                    "refresh_token": refresh_token,
                    "expiry_date": int(time.time() * 1000) + (new_data["expires_in"] * 1000),
                }
            logger.warning(
                f"Gemini token refresh failed with status {resp.status_code}: {resp.text[:100]}"
            )
            return None
        except Exception as e:
            logger.error(f"Failed to refresh Gemini token: {e}")
            return None
