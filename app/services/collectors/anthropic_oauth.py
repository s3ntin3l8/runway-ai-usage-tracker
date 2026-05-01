import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import (
    HealthCalculator,
    PaceCalculator,
    error_card,
    http_request_with_retry,
    human_delta,
)
from app.services.collectors._anthropic_common import (
    ANTHROPIC_WINDOW_NAME_MAP,
    anthropic_model_id_for,
    classify_anthropic_window_type,
)
from app.services.collectors.oauth_base import OAuthBaseCollector
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
        credentials_path: str,
        account_id: str | None = None,
        account_label: str | None = None,
    ):
        super().__init__(
            provider_name=provider_name,
            credentials_path=credentials_path,
            account_id=account_id,
            account_label=account_label,
        )
        self._last_api_fetch = None

    async def _execute_refresh(self, client: httpx.AsyncClient) -> dict | None:
        """Execute the HTTP request to refresh the Claude OAuth token."""
        creds = await self._get_credentials()
        refresh_token = creds.get("claudeAiOauth", {}).get("refreshToken") if creds else None

        if not refresh_token:
            refresh_token = await token_cache.get_token(
                "anthropic", "refresh_token", account_id=self.account_id
            )

        if not refresh_token:
            return None

        # Auto-discover client_id from credentials JSON or id_token
        client_id = settings.CLAUDE_OAUTH_CLIENT_ID
        if creds:
            oauth_payload = creds.get("claudeAiOauth", {})
            client_id = oauth_payload.get("clientId") or oauth_payload.get("client_id") or client_id

            id_token = oauth_payload.get("idToken") or oauth_payload.get("id_token")
            if (not client_id or client_id == settings.CLAUDE_OAUTH_CLIENT_ID) and id_token:
                from app.core.utils import IdentityExtractor

                token_client_id = IdentityExtractor.get_client_id_from_jwt(id_token)
                if token_client_id:
                    client_id = token_client_id

        try:
            resp = await http_request_with_retry(
                client,
                "POST",
                "https://platform.claude.com/v1/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
                headers={
                    "User-Agent": "claude-code/2.1.69",
                    "anthropic-beta": "oauth-2025-04-20",
                },
                timeout=10,
                retry_on_429=False,
            )

            if resp.status_code == 200:
                new_data = resp.json()
                if not creds:
                    creds = {"claudeAiOauth": {}}
                creds["claudeAiOauth"]["accessToken"] = new_data["access_token"]
                creds["claudeAiOauth"]["refreshToken"] = new_data.get(
                    "refresh_token", refresh_token
                )
                creds["claudeAiOauth"]["expiresAt"] = int(time.time() * 1000) + (
                    new_data["expires_in"] * 1000
                )
                await self._store_sidecar_token(
                    "anthropic",
                    new_data["access_token"],
                    new_data.get("refresh_token", refresh_token),
                )
                creds["access_token"] = new_data["access_token"]
                self._clear_refresh_429_backoff()
                return creds
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

    async def _get_claude_oauth(
        self, client: httpx.AsyncClient, token: str
    ) -> list[dict[str, Any]]:
        """
        Fetch Claude quota from Anthropic OAuth API.

        Calls https://api.anthropic.com/api/oauth/usage and returns quota cards.
        Proactively respects 429 backoff to avoid hammering the API.
        """
        # Safety guard: Browser session keys or bundles will 100% fail here with 401.
        # Yield to the Web API scraper strategy instead.
        if token.startswith("sk-ant-sid") or "sessionKey=" in token:
            logger.debug(
                "Skipping Anthropic OAuth API call: provided token is a browser session key or bundle."
            )
            return []

        url = "https://api.anthropic.com/api/oauth/usage"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-code/2.1.69",
            "anthropic-beta": "oauth-2025-04-20",
        }
        name_map = ANTHROPIC_WINDOW_NAME_MAP

        # --- HTTP request ---
        try:
            resp = await http_request_with_retry(client, "GET", url, headers=headers, timeout=10.0)
        except httpx.HTTPError as e:
            logger.error(f"Claude OAuth request failed: {e}")
            return [
                error_card("Claude Pro", "🟠", f"Conn Fail: {str(e)[:20]}", error_type="timeout")
            ]

        if resp.status_code == 401:
            return [
                error_card(
                    "Claude Pro",
                    "🟠",
                    "Expired/Invalid Token (OAuth)",
                    error_type="auth_failed",
                )
            ]

        if resp.status_code == 429:
            logger.info(
                "Anthropic API returned 429. Attempting aggressive recovery via token rotation..."
            )
            # Skip bypass if the token endpoint itself is in backoff — hammering it
            # will only burn refresh tokens and escalate the rate limit.
            if self._is_refresh_backoff_active():
                logger.warning("Token refresh endpoint in backoff, skipping aggressive recovery")
            else:
                # Attempt to get a fresh token (even if not expired)
                try:
                    new_token = await self._get_valid_token(client, force_refresh=True)
                except httpx.HTTPError:
                    new_token = None
                if new_token and new_token != token:
                    logger.info("Successfully cycled Anthropic token. Retrying usage request...")
                    headers["Authorization"] = f"Bearer {new_token}"
                    try:
                        resp = await http_request_with_retry(
                            client, "GET", url, headers=headers, timeout=15.0
                        )
                    except httpx.HTTPError as e:
                        logger.warning(f"Aggressive recovery request failed: {e}")
                    else:
                        if resp.status_code == 200:
                            logger.info("Aggressive recovery successful: Usage data retrieved.")
                        else:
                            logger.warning(
                                f"Aggressive recovery failed: Retry returned {resp.status_code}"
                            )
                else:
                    logger.warning("Aggressive recovery failed: Could not obtain a fresh token.")

        # Re-check status after potential retry
        if resp.status_code == 429:
            # Fallback: set retry-after for SmartCollector backoff
            retry_after = resp.headers.get("Retry-After")
            # Ensure a minimum floor of 300s even if header says 0 to avoid hammering
            wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
            wait_sec = max(wait_sec, 300)

            self._last_retry_after = wait_sec
            logger.warning(f"Anthropic API still rate limited. Retry-After: {wait_sec}s")
            return [
                error_card(
                    "Claude Pro",
                    "🟠",
                    f"Rate Limited (429) - Try in {wait_sec / 60:.1f}m",
                    error_type="rate_limited",
                )
            ]

        if resp.status_code != 200:
            return [
                error_card(
                    "Claude Pro", "🟠", f"API Error {resp.status_code}", error_type="api_error"
                )
            ]

        # --- Parse response ---
        try:
            data = resp.json()
        except (ValueError, KeyError) as e:
            logger.error(f"Claude OAuth response parse failed: {e}")
            return [error_card("Claude Pro", "🟠", "Invalid API response", error_type="api_error")]
        creds = await self._get_credentials()

        # Attempt to fetch account info from API if missing in local credentials
        api_account_info = {}
        if not creds or not creds.get("oauthAccount", {}).get("emailAddress"):
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

        return self._parse_oauth_response(data, name_map, creds, api_account_info)

    def _extract_identity_from_oauth(self, data: dict[str, Any] | None) -> str:
        """Extract account identity string from OAuth API response or credentials file."""
        if not data:
            return ""

        # api_account_info structure
        email = data.get("email")
        org = data.get("organization")
        if email and org:
            return f"{email} @ {org}"
        if email:
            return email
        if org:
            return f"org: {org}"

        # .claude.json credentials structure: oauthAccount.emailAddress
        oauth_account = data.get("oauthAccount", {})
        if oauth_account:
            email = oauth_account.get("emailAddress", "") or oauth_account.get("email", "")
            if email:
                return email

        # OAuth API response structure (fallback)
        account = data.get("account", {})
        email = account.get("email", "")
        org = account.get("organization", "")

        if email and org:
            return f"{email} @ {org}"
        if email:
            return email
        if org:
            return f"org: {org}"
        return ""

    def _get_local_config_hints(self) -> dict[str, Any]:
        """Read supplementary billing hints from ~/.claude.json if available."""

        from app.core.config import is_local_collector_enabled

        if not is_local_collector_enabled():
            return {}
        path = os.path.expanduser("~/.claude.json")
        try:
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _parse_oauth_response(
        self,
        data: dict[str, Any],
        name_map: dict[str, str],
        creds: dict | None = None,
        api_account_info: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Parse OAuth API response into standardized quota cards."""
        results = []
        local_hints = self._get_local_config_hints()

        # Infer plan/tier: Credentials > API Info > Local Config
        tier = api_account_info.get("tier") if api_account_info else None
        if not tier and creds:
            oauth = creds.get("claudeAiOauth", {})
            raw_sub = oauth.get("subscriptionType")
            raw_tier = oauth.get("rateLimitTier")

            if raw_sub:
                tier = str(raw_sub).capitalize()
            elif raw_tier:
                # Match pro/max/team/free followed by optional multiplier like 5x or 20x
                match = re.search(r"(pro|max|team|free)[\s_]*(\d+x)?", raw_tier.lower())
                if match:
                    base = match.group(1).capitalize()
                    mult = match.group(2)
                    tier = f"{base} {mult}" if mult else base
                else:
                    tier_map = {
                        "tier_0": "Free",
                        "tier_1": "Pro",
                        "tier_2": "Max",
                        "tier_3": "Team",
                        "tier_4": "Enterprise",
                        "tier_5": "Enterprise",
                        "default_claude_ai": "Pro",
                    }
                    tier = tier_map.get(raw_tier.lower(), raw_tier.capitalize())

        if not tier:
            local_tier = local_hints.get("billing_tier") or local_hints.get("tier")
            if local_tier:
                tier = str(local_tier).capitalize()

        # Final fallback from data (if API ever includes it)
        if not tier:
            account = data.get("account", {})
            plan = account.get("plan", "")
            tier = plan.capitalize() if plan else None

        # Resolve identity
        identity_str = ""
        if api_account_info:
            identity_str = self._extract_identity_from_oauth(api_account_info)

        if not identity_str:
            identity_str = self._extract_identity_from_oauth(data)

        if not identity_str and creds:
            identity_str = self._extract_identity_from_oauth(creds)

        if not identity_str and local_hints:
            identity_str = self._extract_identity_from_oauth(local_hints)

        identity_suffix = f" | {identity_str}" if identity_str else ""

        # Persist identity to collector
        if identity_str:
            self.account_label = identity_str

        # Guaranteed keys to show even if null from API
        core_keys = [
            "five_hour",
            "seven_day",
            "seven_day_sonnet",
        ]
        all_keys = list(data.keys())
        for ck in core_keys:
            if ck not in all_keys:
                all_keys.append(ck)

        def sort_key(k):
            try:
                return list(name_map.keys()).index(k)
            except ValueError:
                return 999

        for key in sorted(all_keys, key=sort_key):
            if key == "account":
                continue

            usage = data.get(key)
            if usage is None:
                if key in core_keys:
                    usage = {"utilization": 0.0, "resets_at": None}
                else:
                    continue

            u_type = name_map.get(key, key.replace("_", " ").title())

            # 1. Handle Balance/Currency fields (Prepaid or Specific Balance)
            if key in ["current_balance", "available_balance", "balance", "credits"]:
                try:
                    bal = float(usage) if usage is not None else 0.0
                    results.append(
                        {
                            "service_name": "Claude",
                            "variant": u_type,
                            "icon": "💰",
                            "remaining": f"${bal:.2f}",
                            "unit": "USD",
                            "reset": "Prepaid",
                            "health": HealthCalculator.from_balance(bal),
                            "pace": "Manual Top-up",
                            "detail": f"Current Balance: ${bal:.2f} [OAuth]{identity_suffix}",
                            "used_value": 0.0,
                            "limit_value": bal,
                            "unit_type": "currency",
                            "window_type": "rolling",
                            "model_id": None,
                            "data_source": self.DATA_SOURCE_API,
                            "input_source": getattr(self, "_current_input_source", "unknown"),
                            "tier": tier,
                            "account_label": identity_str,
                            "usage_url": "https://claude.ai/settings/usage",
                            "updated_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    # Only emit one balance card even if multiple keys exist
                    break
                except (ValueError, TypeError):
                    pass
                continue

            # Skip overage when extra_usage is also present to avoid duplicate
            # spend/limit cards (Web API parser uses the same merge logic).
            if key == "overage" and data.get("extra_usage") is not None:
                continue

            if not isinstance(usage, dict):
                continue

            # 2. Handle Spend/Limit windows (Overage/Spent)
            raw_spend = usage.get("spend")
            raw_limit = usage.get("limit")
            if raw_limit is not None and float(raw_limit) > 0:
                spend = float(raw_spend) if raw_spend is not None else 0.0
                limit = float(raw_limit)
                remaining = max(0.0, limit - spend)
                results.append(
                    {
                        "service_name": "Claude",
                        "variant": u_type,
                        "icon": "💰",
                        "remaining": f"${remaining:.2f}",
                        "unit": "limit",
                        "reset": "Monthly",
                        "health": HealthCalculator.from_spend(spend, limit),
                        "pace": "Flexible",
                        "detail": f"Spent: ${spend:.2f} / ${limit:.2f} [OAuth]{identity_suffix}",
                        "used_value": spend,
                        "limit_value": limit,
                        "unit_type": "currency",
                        "window_type": "monthly",
                        "model_id": None,
                        "data_source": self.DATA_SOURCE_API,
                        "input_source": getattr(self, "_current_input_source", "unknown"),
                        "tier": tier,
                        "account_label": identity_str,
                        "usage_url": "https://claude.ai/settings/usage",
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )
                continue

            # 3. Handle Standard Percentage windows
            raw_utilization = usage.get("utilization")
            pct_used = float(raw_utilization) if raw_utilization is not None else 0.0
            remaining_pct = 100.0 - pct_used

            reset_raw = usage.get("resets_at") or usage.get("resetsAt")
            reset_at = None
            if reset_raw:
                try:
                    reset_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            w_type = classify_anthropic_window_type(key)
            service_name = "Claude Design" if key == "seven_day_omelette" else "Claude"

            results.append(
                {
                    "service_name": service_name,
                    "icon": "🟠",
                    "remaining": f"{remaining_pct:.1f}%",
                    "unit": "capacity",
                    "reset": human_delta(reset_at),
                    "health": HealthCalculator.from_percentage(pct_used),
                    "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                    "detail": f"{pct_used:.1f}% used [OAuth]{identity_suffix}",
                    "used_value": pct_used,
                    "limit_value": 100.0,
                    "is_unlimited": False,
                    "unit_type": "percent",
                    "window_type": w_type,
                    "model_id": anthropic_model_id_for(key),
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": self.DATA_SOURCE_API,
                    "input_source": getattr(self, "_current_input_source", "unknown"),
                    "tier": tier,
                    "account_label": identity_str,
                    "usage_url": "https://claude.ai/settings/usage",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )

        return (
            results
            if results
            else [error_card("Claude Pro", "🟠", "No quota data", error_type="parse_error")]
        )
