"""
Anthropic (Claude) quota collector with 4-tier fallback strategy and automatic token refresh.

Collection Strategy:
1. Primary: OAuth API endpoint (https://api.anthropic.com/api/oauth/usage)
   - Requires CLAUDE_CODE_OAUTH_TOKEN environment variable or ~/.claude/.credentials.json
   - Returns real-time usage across multiple quota windows (5h, 7d, 7d-sonnet, 7d-opus, extra)
   - Implements caching (10 min TTL) to avoid rate limiting
   - Automatic token refresh when expired (via platform.claude.com)

2. Secondary: Web API via Chrome cookies (https://claude.ai/api/)
    - Extracts sessionKey cookie from Chrome for claude.ai domain
    - Calls Claude web API endpoints to get usage data
    - Same data quality as OAuth (session, weekly, model-specific quotas)

3. Tertiary: Enhanced local cost usage parsing
    - Parses .jsonl files from ~/.claude/projects/ and ~/.config/claude/projects/
    - Supports comma-separated CLAUDE_CONFIG_DIR for multiple roots
    - Tracks all token types: input, cache_read, cache_creation, output
    - Deduplicates streaming chunks by message.id + requestId

4. Quaternary: Error cards when all methods fail
    - Returns descriptive error with failure reason
    - Distinguishes between token expired, rate limited, missing data

Data Caching:
- OAuth results cached for 10 minutes to handle 429 rate limits gracefully
- Cached results tagged with "[Cached]" in detail field
- Falls back to Web API and local logs without repeating failed API calls

Token Refresh:
- Automatic refresh when access token expires (8 hour lifetime)
- Uses platform.claude.com/v1/oauth/token endpoint
- Persists new tokens to ~/.claude/.credentials.json
- Exponential backoff on transient failures
- Terminal failure for invalid_grant errors

Error Handling:
- 401: Token expired/invalid (attempt refresh, then prompt re-authentication)
- 429: Rate limited (use cache if available, fall back to Web API/logs)
- Connection errors: Fall back to next available method
- Missing files/logs: Return error card with helpful message
"""

import asyncio
import os
import re
import glob
import json
import base64
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
import httpx
from app.core.config import settings, get_platform_config_dir
from app.services.credential_provider import credential_provider
from app.core.utils import (
    PaceCalculator,
    human_delta,
    error_card,
    http_request_with_retry,
    safe_write_json,
)
from app.core.browser_cookies import get_claude_session_cookie
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache
from app.services.collectors.oauth_base import OAuthBaseCollector

logger = logging.getLogger(__name__)




class AnthropicCollector(OAuthBaseCollector):
    """Collector for Anthropic (Claude) quota and usage metrics with 4-tier fallback."""

    def __init__(self):
        """Initialize caching for OAuth results and token refresh tracking."""
        # Credentials file path (search multiple locations, default to standard)
        home = os.path.expanduser("~")
        credentials_path = os.path.join(home, ".claude", ".credentials.json")
        platform_cred_path = os.path.join(
            get_platform_config_dir("claude"), ".credentials.json"
        )

        if not os.path.exists(credentials_path) and os.path.exists(platform_cred_path):
            credentials_path = platform_cred_path

        self._name_map = {
            "five_hour": "Session Window",
            "seven_day": "Weekly Window",
            "seven_day_sonnet": "Sonnet Weekly",
            "seven_day_opus": "Opus Weekly",
            "extra_usage": "Extra Usage",
        }

        super().__init__(provider_name="Anthropic", credentials_path=credentials_path)

        self._cached_results = None
        self._last_fetch = None
        self._cache_ttl = 600  # 10 minutes cache to be safe with 429s
        self._refresh_backoff_seconds = 30  # Start with 30s
        self._max_refresh_backoff = 21600  # Max 6 hours
        self._last_statusline_data = {} # Cache for hybrid fallback

    async def _get_current_token(self) -> Optional[str]:
        """Get the current access token."""
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
                    expires_at = datetime.fromtimestamp(
                        expires_at_ms / 1000, tz=timezone.utc
                    )
                    return datetime.now(timezone.utc) >= expires_at
        except Exception as e:
            logger.debug(f"Could not check token expiration: {e}")
        return False

    async def _execute_refresh(self, client: httpx.AsyncClient) -> Optional[Dict]:
        """Execute the HTTP request to refresh the token for Anthropic."""
        creds = await self._get_credentials()
        refresh_token = (
            creds.get("claudeAiOauth", {}).get("refreshToken") if creds else None
        )

        if not refresh_token:
            refresh_token = await token_cache.get_token("anthropic", "refresh_token")

        if not refresh_token:
            return None

        # Auto-discover client_id from credentials JSON or id_token if not specified
        client_id = settings.CLAUDE_OAUTH_CLIENT_ID
        if creds:
            # Check for explicit clientId field in the JSON from keychain
            oauth_payload = creds.get("claudeAiOauth", {})
            client_id = (
                oauth_payload.get("clientId")
                or oauth_payload.get("client_id")
                or client_id
            )

            # Fallback: extract from id_token if available
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
                            logger.info(
                                f"Auto-discovered Claude Client ID: {client_id[:10]}..."
                            )
                except Exception as e:
                    logger.debug(f"Failed to extract Client ID from Claude id_token: {e}")

        try:
            resp = await client.post(
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

    async def _strategy_statusline(self) -> List[Dict[str, Any]]:
        """
        Choice #1: Read the local Claude statusline file (Fast Path).
        Returns metrics if the file exists and is fresh (< 5 mins old).
        """
        if not settings.LOCAL_COLLECTOR_ENABLED:
            return []

        path = settings.CLAUDE_STATUSLINE_PATH
        try:
            if not os.path.exists(path):
                return []

            # Freshness check (5 minutes)
            mtime = os.path.getmtime(path)
            if (time.time() - mtime) > 300:
                logger.debug(f"Claude statusline file is stale ({int(time.time() - mtime)}s old)")
                return []

            with open(path, "r") as f:
                data = json.load(f)

            self._last_statusline_data = data
            return self._parse_statusline_response(data)
        except Exception as e:
            logger.debug(f"Failed to read Claude statusline: {e}")
            return []

    def _parse_statusline_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse statusline.json into standardized quota cards."""
        results = []
        now = datetime.now(timezone.utc)
        
        # 1. Rate Limits
        limits = data.get("rate_limits", {})
        for key, info in limits.items():
            u_type = self._name_map.get(key, key.replace("_", " ").title())
            pct_used = float(info.get("used_percentage", 0.0))
            reset_ts = info.get("resets_at")
            reset_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc) if reset_ts else None
            
            results.append({
                "service": f"Claude ({u_type})",
                "icon": "🟠",
                "remaining": f"{(100 - pct_used):.1f}%",
                "unit": "capacity",
                "reset": human_delta(reset_at),
                "health": "good" if pct_used < 70 else "warning" if pct_used < 90 else "critical",
                "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                "detail": f"{pct_used:.1f}% used [Statusline]",
                "used_value": pct_used,
                "limit_value": 100.0,
                "unit_type": "percent",
                "reset_at": reset_at.isoformat() if reset_at else None,
                "data_source": "statusline",
                "updated_at": now.isoformat(),
            })

        # 2. Session Context (Tokens/Cost)
        context = data.get("context_window", {})
        if context:
            input_tokens = context.get("total_input_tokens", 0)
            output_tokens = context.get("total_output_tokens", 0)
            total = input_tokens + output_tokens
            max_tokens = context.get("max_tokens", 200000)
            pct = (total / max_tokens * 100) if max_tokens > 0 else 0
            
            results.append({
                "service": "Claude (Session Tokens)",
                "icon": "🪙",
                "remaining": f"{total:,}",
                "unit": f"/ {max_tokens:,}",
                "reset": data.get("model", {}).get("display_name", "Sonnet"),
                "health": "good",
                "pace": "Active",
                "detail": f"IN: {input_tokens:,} | OUT: {output_tokens:,} [Statusline]",
                "used_value": float(total),
                "limit_value": float(max_tokens),
                "unit_type": "tokens",
                "data_source": "statusline",
                "updated_at": now.isoformat(),
            })

        cost = data.get("cost", {})
        if cost and cost.get("total_cost_usd", 0) > 0:
            total_cost = cost.get("total_cost_usd")
            results.append({
                "service": "Claude (Session Cost)",
                "icon": "💰",
                "remaining": f"${total_cost:.2f}",
                "unit": "USD",
                "reset": "This Session",
                "health": "good",
                "pace": "Stable",
                "detail": f"+{cost.get('total_lines_added', 0)} / -{cost.get('total_lines_deleted', 0)} lines [Statusline]",
                "data_source": "statusline",
                "updated_at": now.isoformat(),
            })

        return results

    def _fallback_strategies(self) -> List[Any]:
        """Return the fallback strategies for Claude (Web, CLI, Logs)."""
        return [
            self._get_claude_via_web_api,
            self._strategy_cli_pty,
            self._strategy_local_enhanced,
        ]

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Hybrid strategy: Statusline (Fast) + OAuth (Full)."""
        results = []
        
        # 1. Try Statusline first (Choice #1)
        statusline_res = await self._strategy_statusline()
        results.extend(statusline_res)
        
        # 2. Try OAuth for full coverage
        token = await self._get_valid_token(client)
        if token:
            oauth_res = await self._get_claude_oauth_with_cache(client, token)
            
            # Reactive 401 handling
            is_401 = any("Expired/Invalid Token" in str(r.get("detail", "")) for r in oauth_res)
            if is_401 and not self._terminal_failure:
                async with self._refresh_lock:
                    new_creds = await self._execute_refresh(client)
                    if new_creds:
                        new_token = new_creds.get("claudeAiOauth", {}).get("accessToken")
                        self._persist_credentials(new_creds)
                        oauth_res = await self._get_claude_oauth(client, new_token)
            
            # Merge: Add API results that aren't already covered by statusline
            statusline_keys = {r["service"] for r in statusline_res}
            for r in oauth_res:
                if r["service"] not in statusline_keys:
                    results.append(r)

        return results

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return descriptive error card context when all strategies fail."""
        if credential_provider.get_claude_token():
            return [
                error_card(
                    "Claude Pro",
                    "🟠",
                    "No data — OAuth failed & Logs empty",
                    error_type="missing_config",
                )
            ]

        if await self._has_web_cookie():
            return [
                error_card(
                    "Claude Pro",
                    "🟠",
                    "No data — Web API failed & Logs empty",
                    error_type="missing_config",
                )
            ]

        return [
            error_card(
                "Claude Pro",
                "🟠",
                "No data — Set CLAUDE_CODE_OAUTH_TOKEN or login to claude.ai",
                error_type="missing_config",
            )
        ]


    async def _has_web_cookie(self) -> bool:
        """Check if a web cookie is available without making API calls."""
        return await asyncio.to_thread(get_claude_session_cookie) is not None

    async def _get_claude_oauth_with_cache(self, client: httpx.AsyncClient, token: str):
        """
        Fetch Claude OAuth usage with caching.

        Caches ALL results (success AND errors like 429) for 10 minutes to avoid
        hammering the API when rate limited. Falls back to Web API/logs when cached
        error is returned.

        Args:
            client: httpx.AsyncClient for making requests
            token: OAuth token for Anthropic API

        Returns:
            List[Dict[str, Any]]: Quota cards or error card if fetch fails
        """
        now = datetime.now(timezone.utc)

        # Check cache - works for both success AND error results (check is not None for empty lists)
        if self._cached_results is not None and self._last_fetch:
            if (now - self._last_fetch).total_seconds() < self._cache_ttl:
                return self._cached_results

        res = await self._get_claude_oauth(client, token)

        # Cache ALL results (success AND errors) to avoid hammering API
        self._cached_results = res
        self._last_fetch = now

        return res

    async def _get_claude_oauth(self, client: httpx.AsyncClient, token: str):
        """
        Fetch Claude quota from Anthropic OAuth API.

        Calls https://api.anthropic.com/api/oauth/usage to get real-time usage
        across multiple quota windows (5h, 7d, 7d-sonnet, 7d-opus, extra).

        Handles errors gracefully:
        - 401: Invalid/expired token
        - 429: Rate limited (will be retried by http_request_with_retry)
        - Other: Connection or server error

        Args:
            client: httpx.AsyncClient for making requests
            token: OAuth bearer token

        Returns:
            List[Dict[str, Any]]: List of quota cards, one per window, or error card
        """
        url = "https://api.anthropic.com/api/oauth/usage"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-code/2.1.69",
            "anthropic-beta": "oauth-2025-04-20",
        }

        # Mapping for human-friendly names
        name_map = {
            "five_hour": "Session Window",
            "seven_day": "Weekly Window",
            "seven_day_sonnet": "Sonnet Weekly",
            "seven_day_opus": "Opus Weekly",
            "extra_usage": "Extra Usage",
        }

        try:
            # Use retry logic for rate limit handling
            resp = await http_request_with_retry(
                client, "GET", url, headers=headers, timeout=10.0
            )

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
                return [
                    error_card(
                        "Claude Pro",
                        "🟠",
                        "Rate Limited (429) - max retries exceeded",
                        error_type="rate_limited",
                    )
                ]
            if resp.status_code != 200:
                return [
                    error_card(
                        "Claude Pro",
                        "🟠",
                        f"API Error {resp.status_code}",
                        error_type="api_error",
                    )
                ]

            data = resp.json()
            creds = await self._get_credentials()
            return self._parse_oauth_response(data, name_map, creds)

        except Exception as e:
            logger.error(f"Claude OAuth collection failed: {e}")
            return [
                error_card(
                    "Claude Pro",
                    "🟠",
                    f"Conn Fail: {str(e)[:20]}",
                    error_type="timeout",
                )
            ]

    def _extract_identity_from_oauth(self, data: Dict[str, Any]) -> str:
        """Extract account identity from OAuth API response for display in detail field."""
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
        if not settings.LOCAL_COLLECTOR_ENABLED:
            return {}
        path = os.path.expanduser("~/.claude.json")
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    data = json.load(f)
                    return data
        except Exception:
            pass
        return {}

    def _parse_oauth_response(
        self, data: Dict[str, Any], name_map: Dict[str, str], creds: Optional[Dict] = None
    ) -> List[Dict[str, Any]]:
        """Parse OAuth API response into quota cards with local config hints."""
        results = []
        
        # Load local config hints (~/.claude.json)
        local_hints = self._get_local_config_hints()

        # Extract identity and tier once for all cards
        identity_str = self._extract_identity_from_oauth(data)
        identity_suffix = f" | {identity_str}" if identity_str else ""

        # Infer plan/tier (Priority: Credentials > Local Config > API)
        tier = None
        if creds:
            raw_tier = creds.get("claudeAiOauth", {}).get("rateLimitTier")
            if raw_tier:
                tier_map = {
                    "tier_0": "Free",
                    "tier_1": "Pro",
                    "tier_2": "Max",
                    "tier_3": "Team",
                    "tier_4": "Enterprise",
                    "tier_5": "Enterprise",
                }
                tier = tier_map.get(raw_tier.lower(), raw_tier.capitalize())
        
        # Supplement with local config if still missing or to override
        if not tier:
            local_tier = local_hints.get("billing_tier") or local_hints.get("tier")
            if local_tier:
                tier = str(local_tier).capitalize()

        if not tier:
            account = data.get("account", {})
            plan = account.get("plan", "")
            tier = plan.capitalize() if plan else None

        # Guaranteed keys to process even if null from API
        core_keys = ["five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"]

        # Combine API keys with our core keys to ensure we show everything
        all_keys = list(data.keys())
        for ck in core_keys:
            if ck not in all_keys:
                all_keys.append(ck)

        # Sort using name_map order
        def sort_key(k):
            try:
                return list(name_map.keys()).index(k)
            except ValueError:
                return 999

        sorted_keys = sorted(all_keys, key=sort_key)

        for key in sorted_keys:
            # Skip non-quota metadata like 'account'
            if key == "account":
                continue

            usage = data.get(key)

            # If the API returned null (or hasn't returned it yet), treat as 0 utilization
            if usage is None:
                usage = {"utilization": 0.0, "resets_at": None}

            # If it's a dict but missing utilization (like extra_usage when disabled), treat it as null
            if not isinstance(usage, dict):
                continue

            u_type = name_map.get(key, key.replace("_", " ").title())

            # IMPORTANT: Handle null utilization value explicitly (null -> 0.0)
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

            results.append(
                {
                    "service": f"Claude ({u_type})",
                    "icon": "🟠",
                    "remaining": f"{remaining_pct:.1f}%",
                    "unit": "capacity",
                    "reset": human_delta(reset_at),
                    "health": (
                        "good"
                        if pct_used < 70
                        else "warning" if pct_used < 90 else "critical"
                    ),
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
                }
            )

        return (
            results
            if results
            else [
                error_card(
                    "Claude Pro", "🟠", "No quota data", error_type="parse_error"
                )
            ]
        )

    async def _get_claude_via_web_api(
        self, client: httpx.AsyncClient
    ) -> List[Dict[str, Any]]:
        """
        Fetch Claude quota via Web API using Chrome cookies.

        This is a secondary method that extracts the sessionKey cookie from
        Chrome and uses it to call Claude's web API endpoints. This provides
        the same data quality as OAuth but without requiring the OAuth token.

        Endpoints called:
        1. GET /api/organizations - Get organization UUID
        2. GET /api/organizations/{orgId}/usage - Get usage quotas
        3. GET /api/organizations/{orgId}/overage_spend_limit - Get extra usage (optional)

        Args:
            client: httpx.AsyncClient for making requests

        Returns:
            List[Dict[str, Any]]: Quota cards or empty list if cookie unavailable/failed
        """
        # Extract sessionKey cookie from Chrome
        session_key = await asyncio.to_thread(get_claude_session_cookie)
        if not session_key:
            logger.debug("No Claude sessionKey cookie found in Chrome")
            return []

        headers = {"Cookie": f"sessionKey={session_key}"}

        try:
            # Step 1: Get organization ID
            orgs_resp = await client.get(
                "https://claude.ai/api/organizations", headers=headers, timeout=10.0
            )

            if orgs_resp.status_code != 200:
                logger.warning(
                    f"Claude Web API orgs call failed: {orgs_resp.status_code}"
                )
                return []

            orgs_data = orgs_resp.json()
            if not orgs_data or not isinstance(orgs_data, list) or len(orgs_data) == 0:
                logger.warning("No organizations found in Claude Web API response")
                return []

            # Use first organization (usually there's only one)
            org = orgs_data[0]
            org_id = org.get("uuid") or org.get("id")
            if not org_id:
                logger.warning("No organization UUID found in response")
                return []

            # Step 2: Get account info for tier/plan
            account_data = None
            try:
                account_resp = await client.get(
                    "https://claude.ai/api/account", headers=headers, timeout=10.0
                )
                if account_resp.status_code == 200:
                    account_data = account_resp.json()
            except Exception as e:
                logger.debug(f"Could not fetch account info: {e}")

            # Step 3: Get usage data
            usage_resp = await client.get(
                f"https://claude.ai/api/organizations/{org_id}/usage",
                headers=headers,
                timeout=10.0,
            )

            if usage_resp.status_code != 200:
                logger.warning(
                    f"Claude Web API usage call failed: {usage_resp.status_code}"
                )
                return []

            usage_data = usage_resp.json()
            return self._parse_web_api_response(usage_data, org, account_data)

        except httpx.HTTPError as e:
            logger.warning(f"Claude Web API HTTP error: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.warning(f"Claude Web API JSON decode error: {e}")
            return []
        except Exception as e:
            logger.error(f"Claude Web API collection failed: {e}")
            return []

    def _extract_identity_from_web(self, org_data: Dict[str, Any]) -> str:
        """Extract account identity from Web API organization response for display."""
        # Web API org data has different structure - look for membership info
        membership = org_data.get("membership", {})
        user = membership.get("user", {})
        email = user.get("email", "")
        org_name = org_data.get("name", "")

        if email and org_name:
            return f"{email} @ {org_name}"
        elif email:
            return email
        elif org_name:
            return f"org: {org_name}"
        return ""

    def _parse_web_api_response(
        self,
        data: Dict[str, Any],
        org_data: Dict[str, Any] = None,
        account_data: Dict[str, Any] = None,
    ) -> List[Dict[str, Any]]:
        """Parse Web API response into quota cards."""
        results = []

        # Extract identity once for all cards
        identity_str = self._extract_identity_from_web(org_data) if org_data else ""
        identity_suffix = f" | {identity_str}" if identity_str else ""

        # Extract tier from account data
        plan = account_data.get("plan", "") if account_data else ""
        tier = plan.capitalize() if plan else None

        # Map Web API fields to our standard format
        window_map = {
            "session": ("Session Window", "current_window"),
            "weekly": ("Weekly Window", "current_week"),
            "sonnet": ("Sonnet Weekly", "current_week_sonnet"),
            "opus": ("Opus Weekly", "current_week_opus"),
        }

        for window_key, (display_name, api_key) in window_map.items():
            window_data = data.get(api_key)
            if not window_data:
                continue

            # Get usage percentage - null safety added
            raw_pct = window_data.get("percentUsed")
            pct_used = float(raw_pct) if raw_pct is not None else 0.0
            remaining_pct = 100.0 - pct_used

            # Parse reset time
            reset_at = None
            reset_raw = window_data.get("resetsAt")
            if reset_raw:
                try:
                    reset_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            results.append(
                {
                    "service": f"Claude ({display_name})",
                    "icon": "🟠",
                    "remaining": f"{remaining_pct:.1f}%",
                    "unit": "capacity",
                    "reset": human_delta(reset_at),
                    "health": (
                        "good"
                        if pct_used < 70
                        else "warning" if pct_used < 90 else "critical"
                    ),
                    "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                    "detail": f"{pct_used:.1f}% used [Web API]{identity_suffix}",
                    "used_value": pct_used,
                    "limit_value": 100.0,
                    "is_unlimited": False,
                    "unit_type": "percent",
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": "web_api",
                    "tier": tier,
                    "usage_url": "https://claude.ai/settings/usage",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        # Add extra usage if present
        extra_data = data.get("extra_usage") or data.get("overage")
        if extra_data and isinstance(extra_data, dict):
            raw_spend = extra_data.get("spend")
            raw_limit = extra_data.get("limit")
            spend = float(raw_spend) if raw_spend is not None else 0.0
            limit = float(raw_limit) if raw_limit is not None else 0.0

            if limit > 0:
                pct_used = (spend / limit) * 100
                remaining_pct = 100.0 - pct_used
                results.append(
                    {
                        "service": "Claude (Extra Usage)",
                        "icon": "🟠",
                        "remaining": f"${remaining_pct:.0f}%",
                        "unit": "spend",
                        "reset": "Monthly",
                        "health": (
                            "good"
                            if pct_used < 70
                            else "warning" if pct_used < 90 else "critical"
                        ),
                        "pace": "Sustainable",
                        "detail": f"${spend:.2f} / ${limit:.2f} [Web API]{identity_suffix}",
                        "tier": tier,
                        "usage_url": "https://claude.ai/settings/usage",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

        return results

    async def _strategy_cli_pty(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Third tier: CLI PTY fallback."""
        if not settings.LOCAL_COLLECTOR_ENABLED:
            return []
        return await self._collect_via_cli_pty()

    async def _collect_via_cli_pty(self) -> List[Dict[str, Any]]:
        """
        Fetch Claude usage by running the 'claude' CLI and parsing '/usage' output.
        Matches a robust gold standard fallback strategy.
        """
        try:
            # Check if claude CLI is in path
            proc = await asyncio.create_subprocess_exec(
                "which", "claude",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.wait()
            if proc.returncode != 0:
                logger.debug("Claude CLI not found in path, skipping PTY fallback")
                return []

            # Launch claude CLI, send /usage command
            # We use a shell to piping to avoid complex PTY management if possible
            # Standard 'claude' CLI handles piped input for commands
            process = await asyncio.create_subprocess_exec(
                "claude",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # Send /usage and wait for output
            # Note: We append \n to execute. Some versions might need more.
            stdout, stderr = await process.communicate(input=b"/usage\n")
            output = self._strip_ansi(stdout.decode(errors="ignore"))
            
            if not output or not any(x in output.lower() for x in ["usage", "used", "current"]):
                # Try alternative if directly piping /usage doesn't work
                return []

            return self._parse_cli_usage_output(output)

        except Exception as e:
            logger.debug(f"Claude CLI PTY fallback failed: {e}")
            return []

    def _strip_ansi(self, text: str) -> str:
        """Strip ANSI escape codes from string."""
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    def _parse_cli_usage_output(self, output: str) -> List[Dict[str, Any]]:
        """Parse the text output of 'claude /usage' into quota cards."""
        results = []
        now = datetime.now(timezone.utc)

        # Look for patterns like:
        # "Current session: 42% used (resets in 2h 15m)"
        # "Current week: 10% used (resets in 3d 4h)"
        
        # Regex for percentage and optional reset time
        # Example: "42% used", "10% used (resets in 2h)"
        usage_re = re.compile(
            r"(Current\s+(?:session|week|window))\s*[:\s-]*\s*(\d+(?:\.\d+)?)\s*%\s*used(?:\s*\(resets\s+in\s+([^)]+)\))?",
            re.IGNORECASE
        )

        matches = usage_re.finditer(output)
        for match in matches:
            label_raw = match.group(1).strip().title()
            pct_used = float(match.group(2))
            reset_str = match.group(3)
            
            # Map labels to our standard names
            label_map = {
                "Current Session": "Session Window",
                "Current Week": "Weekly Window",
                "Current Window": "Session Window"
            }
            u_type = label_map.get(label_raw, label_raw)
            remaining_pct = 100.0 - pct_used

            # Approximate reset_at from "in 2h 15m" etc.
            reset_at = None
            if reset_str:
                # Basic duration parsing (h, m, d)
                delta = timedelta()
                d_match = re.search(r'(\d+)\s*d', reset_str)
                h_match = re.search(r'(\d+)\s*h', reset_str)
                m_match = re.search(r'(\d+)\s*m', reset_str)
                
                if d_match: delta += timedelta(days=int(d_match.group(1)))
                if h_match: delta += timedelta(hours=int(h_match.group(1)))
                if m_match: delta += timedelta(minutes=int(m_match.group(1)))
                
                if delta.total_seconds() > 0:
                    reset_at = now + delta

            results.append({
                "service": f"Claude ({u_type})",
                "icon": "🟠",
                "remaining": f"{remaining_pct:.1f}%",
                "unit": "capacity",
                "reset": human_delta(reset_at) if reset_at else "Unknown",
                "health": "good" if pct_used < 70 else "warning" if pct_used < 90 else "critical",
                "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                "detail": f"{pct_used:.1f}% used [CLI PTY]",
                "used_value": pct_used,
                "limit_value": 100.0,
                "unit_type": "percent",
                "reset_at": reset_at.isoformat() if reset_at else None,
                "data_source": "cli",
                "updated_at": now.isoformat(),
            })

        return results

    async def _strategy_local_enhanced(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Fourth tier: Enhanced local logs fallback."""
        if not settings.LOCAL_COLLECTOR_ENABLED:
            return []
        return await self._get_claude_local_enhanced()

    async def _get_claude_local_enhanced(self) -> List[Dict[str, Any]]:
        """
        Enhanced fallback: Parse Claude usage from local project logs.
        Offloads blocking file I/O to a thread to avoid blocking the event loop.
        """
        return await asyncio.to_thread(self._get_claude_local_enhanced_sync)

    def _get_claude_local_enhanced_sync(self) -> List[Dict[str, Any]]:
        """
        Synchronous implementation of local log parsing.
        Called via asyncio.to_thread — must not be awaited directly.

        Scans multiple config directories for .jsonl files and tracks all
        token types including cache reads and cache creation.

        Features:
        - Multiple config roots (CLAUDE_CONFIG_DIR comma-separated)
        - All token types: input, cache_read, cache_creation, output
        - Deduplication by message.id + requestId
        - 5-hour sliding window to match OAuth behavior

        Data Source:
        - Locations: CLAUDE_CONFIG_DIR or defaults (~/.claude/projects, ~/.config/claude/projects)
        - Format: JSONL with entries containing usage field

        Returns:
            List[Dict[str, Any]]: Single card with total tokens or None if logs unavailable
        """
        # Get config directories to scan
        config_dirs = self._get_config_dirs()

        # Find all .jsonl files across all config directories
        all_files = []
        for projects_dir in config_dirs:
            files = glob.glob(f"{projects_dir}/**/*.jsonl", recursive=True)
            all_files.extend(files)

        if not all_files:
            logger.debug(f"No Claude project log files found in any config directory")
            return []

        # Read credentials file for tier info
        tier = None
        try:
            if os.path.exists(self._credentials_path):
                with open(self._credentials_path, "r") as f:
                    data = json.load(f)
                    plan = data.get("account", {}).get("plan", "").lower()
                    if plan:
                        tier = plan.capitalize()
        except Exception as e:
            logger.debug(f"Could not read tier from credentials: {e}")

        # 5-hour window to match OAuth session window
        # Default to pro limit if we can't determine tier (safer assumption for limits)
        limit = (
            settings.CLAUDE_FREE_LIMIT if tier == "Free" else settings.CLAUDE_PRO_LIMIT
        )
        cutoff = datetime.now(timezone.utc) - timedelta(hours=5)

        # Track tokens and deduplicate
        total_tokens = 0
        seen_messages = set()  # For deduplication: (message_id, request_id)
        oldest: Optional[datetime] = None

        for fpath in all_files:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Only process assistant messages with usage
                        if entry.get("type") != "assistant":
                            continue

                        # Parse timestamp
                        ts_raw = entry.get("timestamp")
                        if not ts_raw:
                            continue

                        try:
                            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        except ValueError:
                            continue

                        if ts < cutoff:
                            continue

                        # Deduplicate by message.id + requestId
                        msg_data = entry.get("message", {})
                        msg_id = msg_data.get("id", "")
                        request_id = msg_data.get("requestId", "")
                        dedup_key = (msg_id, request_id)

                        if dedup_key in seen_messages:
                            continue
                        seen_messages.add(dedup_key)

                        # Sum all token types
                        usage = msg_data.get("usage", {})
                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        cache_read = usage.get("cache_read_tokens", 0)
                        cache_creation = usage.get("cache_creation_tokens", 0)

                        total_tokens += (
                            input_tokens + output_tokens + cache_read + cache_creation
                        )

                        if not oldest or ts < oldest:
                            oldest = ts

            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(f"Error reading Claude log file {fpath}: {e}")
                continue

        # Calculate remaining and percentage
        remaining = max(0, limit - total_tokens)
        pct = (total_tokens / limit * 100) if limit > 0 else 0
        reset_at = (oldest + timedelta(hours=5)) if oldest else None

        return [
            {
                "service": "Claude Pro",
                "icon": "🟠",
                "remaining": f"{remaining:,}",
                "unit": "tokens / 5h",
                "reset": human_delta(reset_at),
                "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                "detail": f"{total_tokens:,} / {limit:,} [Local Logs] | cli-local",
                "used_value": float(total_tokens),
                "limit_value": float(limit),
                "is_unlimited": False,
                "tier": tier,
                "unit_type": "tokens",
                "reset_at": reset_at.isoformat() if reset_at else None,
                "data_source": "local",
                "usage_url": "https://claude.ai/settings/usage",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ]

    def _get_config_dirs(self) -> List[str]:
        """
        Get list of Claude config directories to scan.

        Checks CLAUDE_CONFIG_DIR environment variable first (supports comma-separated paths),
        then falls back to default locations.

        Returns:
            List[str]: List of directory paths that exist
        """
        dirs = []

        # Priority 1: CLAUDE_CONFIG_DIR (comma-separated)
        config_env = os.getenv("CLAUDE_CONFIG_DIR", "")
        if config_env:
            for path in config_env.split(","):
                path = path.strip()
                if path and os.path.isdir(path):
                    # Append /projects if not already present
                    projects_path = (
                        os.path.join(path, "projects")
                        if not path.endswith("/projects")
                        else path
                    )
                    if os.path.isdir(projects_path):
                        dirs.append(projects_path)

        # Priority 2: Default locations (platform-aware)
        default_paths = [
            os.path.join(get_platform_config_dir("claude"), "projects"),
            os.path.expanduser("~/.config/claude/projects"),  # Legacy/Generic Linux
            os.path.expanduser("~/.claude/projects"),  # Legacy/Direct home
        ]

        for path in default_paths:
            if os.path.isdir(path) and path not in dirs:
                dirs.append(path)

        return dirs
