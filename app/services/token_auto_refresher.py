"""Background OAuth token auto-refresh.

Periodically scans the in-memory token cache and proactively calls
`refresh_oauth_token` for any token whose JWT `exp` claim falls within
TOKEN_AUTO_REFRESH_THRESHOLD_SECONDS. Independent of collector polls, so the
Token Health UI never reports "expired" for a token we could have rolled.

Only refreshes tokens for providers in `_REFRESH_ENDPOINTS` whose cache entry
carries a `refresh_token`. Opaque tokens (no parseable JWT exp) and
config-sourced API keys are left untouched.
"""

import asyncio
import logging
import time

from app.core.utils import IdentityExtractor
from app.services.token_cache import token_cache
from app.services.token_refresher import (
    _REFRESH_ENDPOINTS,
    persist_to_local_file,
    refresh_oauth_token,
)

logger = logging.getLogger(__name__)


class TokenAutoRefresher:
    """Background task that proactively refreshes near-expiry OAuth tokens."""

    def __init__(self, interval_seconds: int, threshold_seconds: int):
        self._interval = interval_seconds
        self._threshold = threshold_seconds
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Token auto-refresher started: interval={self._interval}s, "
            f"threshold={self._threshold}s before exp"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_loop(self) -> None:
        # Run an initial pass immediately so a startup-time expired token gets
        # rolled before the first 5-minute interval. The cache is empty at
        # process start but populates as soon as the first poller tick lands.
        while self._running:
            try:
                await self.refresh_due()
            except Exception as e:
                logger.warning(f"Token auto-refresh loop error: {e}")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return

    async def refresh_due(self) -> int:
        """Refresh every cached token whose exp falls inside the threshold.

        Returns the number of tokens successfully refreshed (useful for tests).
        """
        # Evict any already-expired tokens that carry no refresh_token first —
        # they can never be rolled, so leaving them keeps Token Health stuck on
        # a stale "expired" entry (and the dashboard banner lit).
        await token_cache.purge_expired_unrefreshable()

        accounts = await token_cache.get_all_active_accounts()
        now = time.time()
        refreshed = 0

        for provider, account_id, _label in accounts:
            if provider not in _REFRESH_ENDPOINTS:
                continue

            cached = await token_cache.get_with_metadata(provider, account_id)
            if not cached:
                continue
            tokens, meta = cached
            if "refresh_token" not in tokens:
                continue

            exp = self._extract_exp(tokens)
            if exp is None:
                continue  # Opaque token — can't tell when it expires.
            seconds_left = exp - now
            if seconds_left > self._threshold:
                continue

            try:
                new_tokens = await refresh_oauth_token(provider, tokens)
                await token_cache.store(
                    provider,
                    new_tokens,
                    account_id=account_id,
                    account_label=meta.get("account_label"),
                    source=meta.get("source"),
                )
                persist_to_local_file(provider, new_tokens, meta.get("source"))
                refreshed += 1
                logger.info(
                    f"Auto-refreshed {provider}/{account_id} ({seconds_left:.0f}s before expiry)"
                )
            except Exception as e:
                logger.warning(
                    f"Auto-refresh failed for {provider}/{account_id}: {type(e).__name__}: {e}"
                )

        return refreshed

    @staticmethod
    def _extract_exp(tokens: dict[str, str]) -> float | None:
        """Find the JWT `exp` claim on any token field that carries one."""
        for key in ("oauth_token", "access_token", "id_token"):
            tok = tokens.get(key)
            if not tok:
                continue
            payload = IdentityExtractor.extract_jwt_payload(tok)
            exp = payload.get("exp")
            if exp is None:
                continue
            try:
                return float(exp)
            except (TypeError, ValueError):
                continue
        return None


def build_default() -> TokenAutoRefresher:
    """Construct the global instance from current settings (lazy import)."""
    from app.core.config import settings

    return TokenAutoRefresher(
        interval_seconds=settings.TOKEN_AUTO_REFRESH_INTERVAL_SECONDS,
        threshold_seconds=settings.TOKEN_AUTO_REFRESH_THRESHOLD_SECONDS,
    )


token_auto_refresher = build_default()
