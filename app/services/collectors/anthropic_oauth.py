"""
Anthropic (Claude) OAuth token management and API client.

Handles:
- Access token retrieval and expiry checking
- Automatic token refresh with Client ID auto-discovery
- OAuth API calls to https://api.anthropic.com/api/oauth/usage
- Response parsing for quota cards
"""

import json
import base64
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import httpx

from app.core.config import settings
from app.services.credential_provider import credential_provider
from app.core.utils import PaceCalculator, human_delta, error_card, http_request_with_retry
from app.services.token_cache import token_cache
from app.services.collectors.oauth_base import OAuthBaseCollector

logger = logging.getLogger(__name__)


class AnthropicOAuthMixin(OAuthBaseCollector):
    """
    Mixin providing OAuth token management and API collection for Anthropic (Claude).
    Intended to be composed into AnthropicCollector.
    """

    # ──────────────────────────────── Token lifecycle ────────────────────────

    async def _get_current_token(self) -> Optional[str]:
        """Get the current access token from credential provider or sidecar cache."""
        token = credential_provider.get_claude_token()
        if not token:
            token = await token_cache.get_token("anthropic", "oauth_token")
        return token

    async def _is_token_expired(self) -> bool:
        """Check if OAuth token is expired by reading credentials file."""
        try:
            creds = await self._get_credentials()
            if creds:
                expires_at_ms = creds.get("claudeAiOauth", {}).get("expiresAt")
                if expires_at_ms:
                    expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc)
                    return datetime.now(timezone.utc) >= expires_at
        except Exception as e:
            logger.debug(f"Could not check token expiration: {e}")
        return False

    async def _execute_refresh(self, client: httpx.AsyncClient) -> Optional[Dict]:
        """
        Execute the HTTP request to refresh the Claude OAuth token.

        Auto-discovers the OAuth client_id from:
        1. Credentials file/keychain explicit clientId field
        2. id_token JWT payload (azp/aud claim)
        3. CLAUDE_OAUTH_CLIENT_ID setting (default fallback)
        """
        creds = await self._get_credentials()
        refresh_token = creds.get("claudeAiOauth", {}).get("refreshToken") if creds else None

        if not refresh_token:
            refresh_token = await token_cache.get_token("anthropic", "refresh_token")

        if not refresh_token:
            return None

        # Auto-discover client_id from credentials JSON or id_token
        client_id = settings.CLAUDE_OAUTH_CLIENT_ID
        if creds:
            oauth_payload = creds.get("claudeAiOauth", {})
            client_id = (
                oauth_payload.get("clientId")
                or oauth_payload.get("client_id")
                or client_id
            )

            # Fallback: decode id_token JWT to find authorized party
            id_token = oauth_payload.get("idToken") or oauth_payload.get("id_token")
            if (not client_id or client_id == settings.CLAUDE_OAUTH_CLIENT_ID) and id_token:
                try:
                    parts = id_token.split(".")
                    if len(parts) >= 2:
                        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
                        payload = json.loads(
                            base64.urlsafe_b64decode(payload_b64).decode("utf-8")
                        )
                        token_client_id = payload.get("azp") or payload.get("aud")
                        if token_client_id:
                            client_id = token_client_id
                            logger.info(f"Auto-discovered Claude Client ID: {client_id[:10]}...")
                except Exception as e:
                    logger.debug(f"Failed to extract Client ID from Claude id_token: {e}")

        from app.core.utils import http_request_with_retry
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

                # Update sidecar cache
                await self._store_sidecar_token(
                    "anthropic",
                    new_data["access_token"],
                    new_data.get("refresh_token", refresh_token),
                )

                # Update credential_provider cache via proper setter
                credential_provider.update_claude_token(new_data["access_token"])

                return creds
            elif resp.status_code == 400:
                error_data = resp.json()
                if error_data.get("error") == "invalid_grant":
                    logger.error("Terminal OAuth failure (invalid_grant) for Anthropic")
                    self._terminal_failure = True
            return None
        except Exception as e:
            logger.error(f"Failed to refresh Anthropic token: {e}")
            return None

    # ──────────────────────────────── OAuth API client ───────────────────────

    async def _get_claude_oauth_with_cache(
        self, client: httpx.AsyncClient, token: str
    ) -> List[Dict[str, Any]]:
        """
        Fetch Claude OAuth usage with internal 10-minute caching for API calls.
        
        This allows the Statusline to update frequently (via SmartCollector 60s TTL)
        while keeping the expensive/rate-limited API calls on a slower cycle.
        """
        now = datetime.now(timezone.utc)
        
        # Check internal API cache (10 mins)
        if hasattr(self, "_cached_api_results") and self._cached_api_results is not None and self._last_api_fetch:
            if (now - self._last_api_fetch).total_seconds() < 600:
                # If the cached result is a rate limit error, check if backoff expired
                is_rate_limited = any(r.get("error_type") == "rate_limited" for r in self._cached_api_results)
                if is_rate_limited:
                    backoff_until = getattr(self, "_last_429_backoff_until", None)
                    if backoff_until and now < backoff_until:
                        return self._cached_api_results
                    # Backoff expired, fall through to fetch fresh
                else:
                    return self._cached_api_results

        res = await self._get_claude_oauth(client, token)
        
        # Only cache if not a transient connection error
        if not any(r.get("error_type") == "timeout" for r in res):
            self._cached_api_results = res
            self._last_api_fetch = now
            
        return res

    async def _get_claude_oauth(
        self, client: httpx.AsyncClient, token: str
    ) -> List[Dict[str, Any]]:
        """
        Fetch Claude quota from Anthropic OAuth API.

        Calls https://api.anthropic.com/api/oauth/usage and returns quota cards.
        Proactively respects 429 backoff to avoid hammering the API.
        """
        # Proactive backoff check
        now = datetime.now(timezone.utc)
        backoff_until = getattr(self, "_last_429_backoff_until", None)
        if backoff_until and now < backoff_until:
            wait_rem = (backoff_until - now).total_seconds()
            logger.debug(f"Proactively skipping Anthropic API call due to recent 429 (backoff for {wait_rem:.0f}s)")
            return [error_card("Claude Pro", "🟠", f"Rate Limited (429) - Backoff for {wait_rem:.0f}s", error_type="rate_limited")]

        url = "https://api.anthropic.com/api/oauth/usage"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-code/2.1.69",
            "anthropic-beta": "oauth-2025-04-20",
        }
        name_map = {
            "five_hour": "Session Window",
            "seven_day": "Weekly Window",
            "seven_day_sonnet": "Sonnet Weekly",
            "seven_day_opus": "Opus Weekly",
            "extra_usage": "Extra Usage",
        }

        try:
            resp = await http_request_with_retry(client, "GET", url, headers=headers, timeout=10.0)

            if resp.status_code == 401:
                return [error_card("Claude Pro", "🟠", "Expired/Invalid Token (OAuth)", error_type="auth_failed")]
            
            if resp.status_code == 429:
                # Set proactive backoff based on Retry-After or default 5m
                retry_after = resp.headers.get("Retry-After")
                # Default to 5 mins if no header, but trust the header if present
                wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
                self._last_429_backoff_until = now + timedelta(seconds=wait_sec)
                logger.warning(f"Anthropic API returned 429. Proactive backoff set for {wait_sec}s")
                return [error_card("Claude Pro", "🟠", f"Rate Limited (429) - Try in {wait_sec/60:.1f}m", error_type="rate_limited")]
            
            if resp.status_code != 200:
                return [error_card("Claude Pro", "🟠", f"API Error {resp.status_code}", error_type="api_error")]

            # Success: Clear any backoff
            self._last_429_backoff_until = None
            data = resp.json()
            creds = await self._get_credentials()
            return self._parse_oauth_response(data, name_map, creds)

        except Exception as e:
            logger.error(f"Claude OAuth collection failed: {e}")
            return [error_card("Claude Pro", "🟠", f"Conn Fail: {str(e)[:20]}", error_type="timeout")]

    def _extract_identity_from_oauth(self, data: Dict[str, Any]) -> str:
        """Extract account identity string from OAuth API response."""
        account = data.get("account", {})
        email = account.get("email", "")
        org = account.get("organization", "")

        if email and org:
            return f"{email} @ {org}"
        elif email:
            return email
        elif org:
            return f"org: {org}"
        return ""

    def _get_local_config_hints(self) -> Dict[str, Any]:
        """Read supplementary billing hints from ~/.claude.json if available."""
        import os
        if not settings.LOCAL_COLLECTOR_ENABLED:
            return {}
        path = os.path.expanduser("~/.claude.json")
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _parse_oauth_response(
        self,
        data: Dict[str, Any],
        name_map: Dict[str, str],
        creds: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """Parse OAuth API response into standardized quota cards."""
        results = []
        local_hints = self._get_local_config_hints()

        identity_str = self._extract_identity_from_oauth(data)
        identity_suffix = f" | {identity_str}" if identity_str else ""

        # Infer plan/tier: Credentials > Local Config > API
        tier = None
        if creds:
            raw_tier = creds.get("claudeAiOauth", {}).get("rateLimitTier")
            if raw_tier:
                tier_map = {
                    "tier_0": "Free", "tier_1": "Pro", "tier_2": "Max",
                    "tier_3": "Team", "tier_4": "Enterprise", "tier_5": "Enterprise",
                }
                tier = tier_map.get(raw_tier.lower(), raw_tier.capitalize())

        if not tier:
            local_tier = local_hints.get("billing_tier") or local_hints.get("tier")
            if local_tier:
                tier = str(local_tier).capitalize()

        if not tier:
            account = data.get("account", {})
            plan = account.get("plan", "")
            tier = plan.capitalize() if plan else None

        # Guaranteed keys to show even if null from API
        core_keys = ["five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"]
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
                usage = {"utilization": 0.0, "resets_at": None}

            u_type = name_map.get(key, key.replace("_", " ").title())
            
            # 1. Handle Balance/Currency fields (Prepaid or Specific Balance)
            if key in ["current_balance", "available_balance", "balance", "credits"]:
                try:
                    bal = float(usage) if usage is not None else 0.0
                    results.append({
                        "service": f"Claude ({u_type})",
                        "icon": "💰",
                        "remaining": f"${bal:.2f}",
                        "unit": "USD",
                        "reset": "Prepaid",
                        "health": "good" if bal > 5.0 else "warning",
                        "pace": "Manual Top-up",
                        "detail": f"Current Balance: ${bal:.2f} [OAuth]{identity_suffix}",
                        "used_value": 0.0,
                        "limit_value": bal,
                        "unit_type": "currency",
                        "data_source": "oauth",
                        "tier": tier,
                        "usage_url": "https://claude.ai/settings/usage",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                except (ValueError, TypeError):
                    pass
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
                results.append({
                    "service": f"Claude ({u_type})",
                    "icon": "💰",
                    "remaining": f"${remaining:.2f}",
                    "unit": "limit",
                    "reset": "Monthly",
                    "health": "good" if remaining > 5.0 else "warning",
                    "pace": "Flexible",
                    "detail": f"Spent: ${spend:.2f} / ${limit:.2f} [OAuth]{identity_suffix}",
                    "used_value": spend,
                    "limit_value": limit,
                    "unit_type": "currency",
                    "data_source": "oauth",
                    "tier": tier,
                    "usage_url": "https://claude.ai/settings/usage",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
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

            results.append({
                "service": f"Claude ({u_type})",
                "icon": "🟠",
                "remaining": f"{remaining_pct:.1f}%",
                "unit": "capacity",
                "reset": human_delta(reset_at),
                "health": "good" if pct_used < 70 else "warning" if pct_used < 90 else "critical",
                "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                "detail": f"{pct_used:.1f}% used [OAuth]{identity_suffix}",
                "used_value": pct_used,
                "limit_value": 100.0,
                "is_unlimited": False,
                "unit_type": "percent",
                "reset_at": reset_at.isoformat() if reset_at else None,
                "data_source": "oauth",
                "tier": tier,
                "usage_url": "https://claude.ai/settings/usage",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        return results if results else [
            error_card("Claude Pro", "🟠", "No quota data", error_type="parse_error")
        ]
