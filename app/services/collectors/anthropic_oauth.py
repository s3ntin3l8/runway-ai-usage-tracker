import logging
import time
from typing import Any

import httpx

from app.core.utils import (
    error_card,
    http_request_with_retry,
)
from app.services.account_identity import is_placeholder_label
from app.services.claude_oauth_usage import (
    OAuthUsageError,
    billing_model_from_oauth_cards,
    fetch_oauth_usage,
    oauth_identity_string,
    oauth_organization_name,
    oauth_usage_cards,
)
from app.services.collectors._anthropic_common import (
    ANTHROPIC_WINDOW_NAME_MAP,
)
from app.services.collectors.oauth_base import OAuthBaseCollector
from app.services.identity_lookup import claude_profile_email
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class AnthropicOAuthMixin(OAuthBaseCollector):
    """
    Anthropic (Claude) OAuth token management and API client.

    Handles:
    - Access token retrieval and expiry checking
    - Automatic token refresh with Client ID auto-discovery
    - OAuth API calls to https://api.anthropic.com/api/oauth/usage
    - Response parsing for quota cards
    """

    def __init__(
        self,
        provider_name: str,
        account_id: str | None = None,
        account_label: str | None = None,
    ):
        super().__init__(
            provider_name=provider_name,
            account_id=account_id,
            account_label=account_label,
        )
        self._last_api_fetch = None

    async def _execute_refresh(self, client: httpx.AsyncClient) -> dict | None:
        """Execute the HTTP request to refresh the Claude OAuth token."""
        refresh_token = await token_cache.get_token(
            "anthropic", "refresh_token", account_id=self.account_id
        )
        if not refresh_token:
            return None

        from app.services.token_refresher import machine_owns_credential

        selected = token_cache.selected_source("anthropic")
        owner_account = selected[0] if selected else (self.account_id or "default")
        if machine_owns_credential(
            "anthropic",
            {"refresh_token": refresh_token},
            await token_cache.get_source_candidates("anthropic", owner_account),
        ):
            # A machine's Claude Code owns this login. Exchanging its refresh token here
            # rotates it and signs that CLI out; the sidecar re-pushes the renewed token.
            logger.info("Not refreshing a Claude login that a machine's CLI owns")
            return None

        from app.services.token_refresher import _anthropic_refresh_request

        # Same shape as Claude Code's own request (and as ``refresh_oauth_token``): anything else
        # is throttled with a 429. Refresh tokens are single-use, so never retry blindly.
        body, headers = _anthropic_refresh_request(refresh_token, {})

        try:
            resp = await http_request_with_retry(
                client,
                "POST",
                "https://platform.claude.com/v1/oauth/token",
                json=body,
                headers=headers,
                timeout=10,
                retry_on_429=False,
            )

            if resp.status_code == 200:
                new_data = resp.json()
                new_refresh = new_data.get("refresh_token", refresh_token)
                # Return the new expiry along with the tokens: the base class stores them (once).
                # The access token is opaque (no JWT ``exp``), so without ``expiry_date`` the
                # merged cache entry keeps the *old* one and a freshly refreshed token still
                # reads as expired.
                expires_in = new_data.get("expires_in")
                expiry_ms: int | None = (
                    int(time.time() * 1000 + expires_in * 1000)
                    if isinstance(expires_in, int | float) and expires_in > 0
                    else None
                )
                self._clear_refresh_429_backoff()
                result: dict = {
                    "access_token": new_data["access_token"],
                    "refresh_token": new_refresh,
                }
                if expiry_ms is not None:
                    result["expiry_date"] = str(expiry_ms)
                return result
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait_sec = float(retry_after) if retry_after else None
                except (ValueError, TypeError):
                    wait_sec = None
                self._set_refresh_429_backoff(wait_sec)
                return None
            if resp.status_code == 400:
                error_data = resp.json()
                if error_data.get("error") == "invalid_grant":
                    logger.error("Terminal OAuth failure (invalid_grant) for Anthropic")
                    self._terminal_failure = True
            return None
        except Exception as e:
            logger.error(f"Failed to refresh Anthropic token: {e}")
            return None

    async def _pending_identity(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> str | None:
        """The email of whoever holds a token the sidecar couldn't identify, if learnable.

        Only for the identity-verification run, i.e. when pinned to a bundle the sidecar
        marked ``identity_pending``. The holder's own profile names them; the organization
        contact (``/v1/organizations/me``) never does, since it can be an admin. Returned,
        not assigned: ``account_id`` changes how the rest of the call sees its pin, so it is
        set once the call is done (``CollectorManager`` then binds it to that exact source).
        """
        if self.account_id and self.account_id.lower() != "default":
            return None
        meta = token_cache.current_source_metadata("anthropic", self.account_id or "default")
        if not meta or meta.get("identity_pending") is not True:
            return None
        return await claude_profile_email(client, headers)

    async def _get_claude_oauth(
        self, client: httpx.AsyncClient, token: str
    ) -> list[dict[str, Any]]:
        """
        Fetch Claude quota from Anthropic OAuth API.

        Calls https://api.anthropic.com/api/oauth/usage and returns quota cards.
        Proactively respects 429 backoff to avoid hammering the API.
        """
        # The bundle this attempt reads with. A proof belongs to one credential:
        # if the bundle is replaced while the answer is in flight, the answer
        # proves nothing about the credential now in the cache.
        generation_before = self._oauth_bundle_generation()
        try:
            data = await fetch_oauth_usage(client, token)
        except OAuthUsageError as exc:
            if exc.error_type == "rate_limited":
                # The strategy backs off through SmartCollector; the token
                # refreshes near its expiry instead.
                self._last_retry_after = exc.retry_after or 300
                logger.warning(
                    f"Anthropic API still rate limited. Retry-After: {self._last_retry_after}s"
                )
                return [error_card("Claude Pro", "🟠", exc.message, error_type="rate_limited")]
            if exc.error_type == "unsupported_credential":
                logger.debug("Skipping Anthropic OAuth API call: not an OAuth token.")
                return []
            logger.error(f"Claude OAuth request failed: {exc.message}")
            return [error_card("Claude Pro", "🟠", exc.message, error_type=exc.error_type)]

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-code/2.1.69",
            "anthropic-beta": "oauth-2025-04-20",
        }
        name_map = ANTHROPIC_WINDOW_NAME_MAP

        holder = await self._pending_identity(client, headers)

        # Fall back to the organization's account info when the token holder is unknown.
        # Skipped once the holder is known: the organization's contact is not who holds
        # the token.
        api_account_info = {}
        if not holder:
            try:
                org_resp = await http_request_with_retry(
                    client,
                    "GET",
                    "https://api.anthropic.com/v1/organizations/me",
                    headers={**headers, "anthropic-version": "2023-06-01"},
                    timeout=10.0,
                )
                if org_resp.status_code == 200:
                    org_data = org_resp.json()
                    api_account_info["organization"] = org_data.get("name")
                    api_account_info["email"] = org_data.get("contact_email") or org_data.get(
                        "email"
                    )
                    plan = org_data.get("plan")
                    if plan:
                        api_account_info["tier"] = plan.capitalize()
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.debug(f"Failed to fetch Anthropic organization info: {e}")

        cards = self._parse_oauth_response(data, name_map, api_account_info)
        if holder:
            self.account_id = holder
            if is_placeholder_label(self.account_label):
                self.account_label = holder
        self._publish_oauth_billing_proof(cards, generation_before)
        return cards

    @staticmethod
    def _oauth_bundle_generation() -> str | None:
        """The generation of the bundle this attempt reads with, if any.

        The generation names one exact credential bundle, so it is what a
        proof can be bound to: a bundle replaced mid-flight has a different
        generation, and the proof about the old one says nothing about the
        new one.
        """
        selection = token_cache.selected_source("anthropic")
        if selection is None:
            return None
        account_id, source_id = selection
        return token_cache.source_generation("anthropic", account_id, source_id)

    def _publish_oauth_billing_proof(
        self, cards: list[dict[str, Any]], generation_before: str | None
    ) -> None:
        """Publish what this attempt proved about the credential it used.

        An OAuth token has no Claude organization to read, so the model its
        own cards describe is the only proof the strategy can make. It is
        published together with the generation of the bundle that produced
        it, and dropped when that bundle was replaced while the answer was in
        flight: a proof belongs to one credential, and a stale proof can never
        claim anything about the next one.
        """
        model = billing_model_from_oauth_cards(cards)
        if model is None:
            # The absence of a proof is no decision.
            return
        if generation_before is None:
            # Nothing to bind the proof to, so it can claim nothing.
            return
        if self._oauth_bundle_generation() != generation_before:
            return
        self.verified_billing_type = model
        self.verified_source_generation = generation_before

    def _extract_identity_from_oauth(self, data: dict[str, Any] | None) -> str:
        """Extract account identity string from the organization info or OAuth API response."""
        return oauth_identity_string(data)

    def _parse_oauth_response(
        self,
        data: dict[str, Any],
        name_map: dict[str, str],
        api_account_info: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Parse OAuth API response into standardized quota cards.

        The reading itself lives in ``app.services.claude_oauth_usage`` so the
        manual pricing path reads the same payload the same way.
        """
        cards = oauth_usage_cards(
            data,
            name_map,
            api_account_info,
            data_source=self.DATA_SOURCE_API,
            input_source=getattr(self, "_current_input_source", "unknown"),
        )
        name_str = oauth_organization_name(data, api_account_info)
        if name_str and is_placeholder_label(self.account_label):
            self.account_label = name_str
        return cards
