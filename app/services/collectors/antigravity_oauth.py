import logging
import time

import httpx

from app.core.utils import IdentityExtractor
from app.services.collectors.oauth_base import OAuthBaseCollector
from app.services.token_cache import borrowable_entries, token_cache

logger = logging.getLogger(__name__)


def _is_token_expired(tokens: dict) -> bool:
    """True when *tokens* carries a known expiry that has already passed.

    Unknown/opaque expiry (no `exp`/`expiry_date`) returns False — absence of
    a signal is not evidence of expiry.
    """
    exp = IdentityExtractor.exp_from_tokens(tokens)
    return exp is not None and exp < time.time()


class AntigravityOAuthMixin(OAuthBaseCollector):
    """OAuth token management for the Antigravity CLI.

    The token comes only from the token cache: the sidecar reads the agy token file
    (``~/.gemini/antigravity-cli/antigravity-oauth-token``) and pushes it. The server never
    reads that file. agy refreshes its own token in the background on each CLI invocation;
    Runway cannot refresh it independently (no OAuth client_id in the file), so an expired
    token waits for agy to run again and the sidecar to re-push.
    """

    # No OAuth client_id on this side: only the user's agy CLI renews this
    # login. A 401 re-reads the cache (another host's push may be live) but
    # must never log as though the server refreshed the token.
    REFRESHABLE = False

    async def _get_current_token(self) -> str | None:
        """Return the current access token from the token cache."""
        cache_data = await token_cache.get_with_metadata("antigravity", account_id=self.account_id)
        # Which read path answered? A single DEBUG below carries the final
        # outcome; no identifiers here (slot keys are token-derived) — the
        # hit state plus the borrowed entry's source is enough to tell
        # "fresh merged hit" from "expired hit, borrowed newest" from
        # "nothing usable" when diagnosing a 401 incident.
        if cache_data is None:
            slot_state = "missing"
        elif _is_token_expired(cache_data[0]):
            slot_state = "expired"
        else:
            slot_state = "fresh"
        read_outcome = "fresh hit for requested slot"
        # Without an account_id the read above already returned the newest entry.
        if slot_state != "fresh" and self.account_id:
            # Identity-mismatch fallback: the agy token file carries no id_token,
            # so the sidecar-pushed token is cached under a refresh-token-derived
            # hash — NOT the email that seeds self.account_id from LatestUsage, and
            # antigravity has no "default" cache entry to catch the miss. Fall back
            # to the newest cached entry (single Google account → it is the right
            # token). Keep self.account_id as the email; do not adopt the hash.
            #
            # Also covers a present-but-expired email-keyed entry: two sidecars
            # can push tokens for the same account (e.g. this host's agy session
            # lapsed while another host's is still valid), and each push
            # overwrites the single shared email-keyed slot. Prefer the newest
            # entry across ALL cached accounts that isn't itself known-expired —
            # falling back to the bare newest-by-recency only if every entry is
            # expired (nothing better to offer).
            # Never another identified account's token (multi-account).
            candidates = borrowable_entries(
                await token_cache.get_accounts("antigravity"),
                self.account_id,
                provider="antigravity",
            )
            fresh = [a for a in candidates if not _is_token_expired(a["tokens"])]
            pool = fresh or candidates
            if pool:
                newest = min(pool, key=lambda a: a["age"])
                read_outcome = (
                    f"requested slot {slot_state}; borrowed newest "
                    f"{'non-expired' if fresh else 'expired'} entry "
                    f"(source={newest['source']})"
                )
                cache_data = (
                    newest["tokens"],
                    {"account_label": newest["account_label"], "source": newest["source"]},
                )
            else:
                cache_data = None
                read_outcome = f"requested slot {slot_state}; nothing borrowable"
        logger.debug("Antigravity token read: %s", read_outcome)
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
        """Defer freshness to the cache TTL; agy owns the token's renewal."""
        return False

    async def _execute_refresh(self, client: httpx.AsyncClient) -> dict | None:
        # agy owns the token refresh cycle; Runway cannot refresh independently
        # because the OAuth client_id is not stored in the token file.
        return None
