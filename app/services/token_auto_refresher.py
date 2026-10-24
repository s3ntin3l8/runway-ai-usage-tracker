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
from collections.abc import Iterable

from app.core.utils import IdentityExtractor, has_refresh_credential
from app.services.account_identity import canonical_account_id
from app.services.credential_tags import archived_account_pairs_from_configs
from app.services.token_cache import server_may_refresh, token_cache
from app.services.token_refresher import (
    _REFRESH_ENDPOINTS,
    refresh_oauth_token,
)

logger = logging.getLogger(__name__)


def _retired_credentials(
    providers: Iterable[str],
) -> tuple[set[tuple[str, str]], set[str]]:
    """The retired ``(provider, account)`` pairs, and the retired providers.

    A retired *account* is archived: nothing is collected for it, and a
    credential mapped onto it is parked. A *provider* whose every configured
    account is archived is retired as a whole — the collector's own rule stops
    it — so a credential the machine reported for it is dead too: a parked
    mapping deliberately leaves that credential unattributed, so it sits under
    no account of its own.

    A *disabled* account is deliberately not covered: that state is reversible,
    and keeping the credential fresh is what the operator wants when they
    re-enable it.
    """
    wanted = {
        provider_id for provider_id in providers if isinstance(provider_id, str) and provider_id
    }
    if not wanted:
        return set(), set()
    from sqlmodel import Session, col, select

    from app.core.db import engine
    from app.models.db import ProviderConfig

    try:
        with Session(engine) as session:
            rows = list(
                session.exec(
                    select(ProviderConfig).where(
                        col(ProviderConfig.provider_id).in_(sorted(wanted))
                    )
                ).all()
            )
    except Exception as exc:  # pragma: no cover — defensive only
        # A DB hiccup must not stop the refresher from doing its job: without
        # the answer it behaves as it does today (refresh what is cached).
        logger.debug(f"Could not read the retired accounts: {exc}")
        return set(), set()
    archived = archived_account_pairs_from_configs(rows)
    flags_by_provider: dict[str, list[bool]] = {}
    for cfg in rows:
        if isinstance(cfg.provider_id, str) and cfg.provider_id:
            flags_by_provider.setdefault(cfg.provider_id, []).append(bool(cfg.archived))
    retired_providers = {
        provider_id for provider_id, flags in flags_by_provider.items() if flags and all(flags)
    }
    return archived, retired_providers


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
            logger.debug("Token auto-refresher task cancelled during shutdown")
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
        # Strip already-expired OAuth fields that carry no refresh_token first —
        # they can never be rolled. Independent credentials beside them (cookies,
        # API keys) are kept, as is an entry that holds nothing else.
        await token_cache.purge_expired_unrefreshable()

        accounts = await token_cache.get_all_active_accounts()
        now = time.time()
        refreshed = 0

        # One query per pass, not per token: the retired accounts and the
        # retired providers refresh nothing.
        retired_accounts, retired_providers = _retired_credentials(
            {provider for provider, _account, _label in accounts}
        )

        for provider, account_id, _label in accounts:
            if provider in retired_providers:
                logger.debug(f"Not refreshing {provider}/{account_id}: the provider is retired")
                continue
            if (provider, canonical_account_id(account_id)) in retired_accounts:
                logger.debug(f"Not refreshing {provider}/{account_id}: the account is retired")
                continue
            if provider not in _REFRESH_ENDPOINTS:
                continue

            cached = await token_cache.get_with_metadata(provider, account_id)
            if not cached:
                continue
            tokens, meta = cached
            if not has_refresh_credential(tokens):
                continue

            exp = IdentityExtractor.exp_from_tokens(tokens)
            if exp is None:
                continue  # Opaque token — can't tell when it expires.
            seconds_left = exp - now
            if seconds_left > self._threshold:
                continue
            if not await server_may_refresh(
                provider, account_id, tokens, merged_source=meta.get("source")
            ):
                # A machine's CLI owns this credential; rotating its refresh token here
                # would sign that CLI out. The machine renews it and the sidecar re-pushes.
                logger.debug(f"Not refreshing {provider}/{account_id}: renewed by its machine")
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
                await token_cache.apply_refresh_to_sources(provider, account_id, tokens, new_tokens)
                refreshed += 1
                logger.info(
                    f"Auto-refreshed {provider}/{account_id} ({seconds_left:.0f}s before expiry)"
                )
            except Exception as e:
                logger.warning(
                    f"Auto-refresh failed for {provider}/{account_id}: {type(e).__name__}: {e}"
                )

        return refreshed


def build_default() -> TokenAutoRefresher:
    """Construct the global instance from current settings (lazy import)."""
    from app.core.config import settings

    return TokenAutoRefresher(
        interval_seconds=settings.TOKEN_AUTO_REFRESH_INTERVAL_SECONDS,
        threshold_seconds=settings.TOKEN_AUTO_REFRESH_THRESHOLD_SECONDS,
    )


token_auto_refresher = build_default()
