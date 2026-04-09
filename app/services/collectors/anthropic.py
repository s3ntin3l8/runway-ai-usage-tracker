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
import glob
import json
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
import httpx
from app.core.config import settings, get_platform_config_dir
from app.core.utils import PaceCalculator, human_delta, error_card, http_request_with_retry
from app.core.chrome_cookies import get_claude_session_cookie
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

# OAuth client ID used by Claude Code CLI (public identifier)
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


class AnthropicCollector(BaseCollector):
    """Collector for Anthropic (Claude) quota and usage metrics with 4-tier fallback."""
    
    def __init__(self):
        """Initialize caching for OAuth results and token refresh tracking."""
        self._cached_results = None
        self._last_fetch = None
        self._cache_ttl = 600  # 10 minutes cache to be safe with 429s
        
        # Token refresh failure tracking (exponential backoff)
        self._last_refresh_failure = None
        self._refresh_backoff_seconds = 30  # Start with 30s
        self._max_refresh_backoff = 21600  # Max 6 hours
        self._terminal_failure = False  # Set to True on invalid_grant
        
        # Credentials file path (search multiple locations, default to standard)
        home = os.path.expanduser("~")
        self._credentials_path = os.path.join(home, ".claude", ".credentials.json")
        platform_cred_path = os.path.join(get_platform_config_dir("claude"), ".credentials.json")
        
        if not os.path.exists(self._credentials_path) and os.path.exists(platform_cred_path):
            self._credentials_path = platform_cred_path

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Claude quota data using 4-tier fallback strategy.
        
        Tries in order:
        1. OAuth API with caching (env var or sidecar token)
        2. Web API via Chrome cookies (if user logged into claude.ai)
        3. Enhanced local log parsing (multiple config roots, full token tracking)
        4. Return descriptive error if all methods fail
        
        Returns:
            List[Dict[str, Any]]: List of quota cards for each quota window.
        """
        # 1. Try OAuth API (env var or sidecar token)
        token = settings.CLAUDE_CODE_OAUTH_TOKEN
        token_source = "env"
        
        # Check token cache from sidecar if no env token
        if not token:
            token = await token_cache.get_token("anthropic", "oauth_token")
            if token:
                token_source = "sidecar"
                logger.info("Using OAuth token from sidecar cache")
        
        if token:
            oauth_res = await self._get_claude_oauth_with_cache(client, token)
            
            # Check if it's a valid result (not an error card)
            is_error = any(r.get("remaining") == "ERR" for r in oauth_res)
            if not is_error and oauth_res:
                return oauth_res
            
            # Log OAuth failure for debugging
            logger.debug(f"OAuth failed (source: {token_source}), falling back to Web API. Result: {oauth_res}")

        # 2. Try Web API via Chrome cookies
        web_res = await self._get_claude_via_web_api(client)
        if web_res:
            is_error = any(r.get("remaining") == "ERR" for r in web_res)
            if not is_error:
                return web_res
            logger.debug(f"Web API failed, falling back to local logs")

        # 3. Fallback to Enhanced Local Cost Usage
        local_res = await self._get_claude_local_enhanced()
        if local_res:
            # If we fell back due to an error, we could tag it
            if settings.CLAUDE_CODE_OAUTH_TOKEN or await self._has_web_cookie():
                for r in local_res:
                    if "(API Fallback)" not in r.get("detail", ""):
                        r["detail"] += " (API Fallback)"
            return local_res
            
        # 4. Final Fallback: Return error with context
        if settings.CLAUDE_CODE_OAUTH_TOKEN:
            return [error_card("Claude Pro", "🟠", "No data — OAuth failed & Logs empty", error_type="missing_config")]
        
        if await self._has_web_cookie():
            return [error_card("Claude Pro", "🟠", "No data — Web API failed & Logs empty", error_type="missing_config")]
            
        return [error_card("Claude Pro", "🟠", "No data — Set CLAUDE_CODE_OAUTH_TOKEN or login to claude.ai", error_type="missing_config")]

    async def _has_web_cookie(self) -> bool:
        """Check if a web cookie is available without making API calls."""
        return get_claude_session_cookie() is not None

    def _is_token_expired(self, token: str) -> bool:
        """Check if OAuth token is expired by reading credentials file."""
        try:
            if os.path.exists(self._credentials_path):
                with open(self._credentials_path, 'r') as f:
                    data = json.load(f)
                    expires_at_ms = data.get("claudeAiOauth", {}).get("expiresAt")
                    if expires_at_ms:
                        expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc)
                        return datetime.now(timezone.utc) >= expires_at
        except Exception as e:
            logger.debug(f"Could not check token expiration: {e}")
        return False

    def _can_attempt_refresh(self) -> bool:
        """Check if we should attempt token refresh based on failure tracking."""
        if self._terminal_failure:
            logger.info("Token refresh blocked due to terminal failure (invalid_grant)")
            return False
        
        if self._last_refresh_failure:
            elapsed = (datetime.now(timezone.utc) - self._last_refresh_failure).total_seconds()
            if elapsed < self._refresh_backoff_seconds:
                logger.debug(f"Token refresh backed off, retry in {self._refresh_backoff_seconds - elapsed:.0f}s")
                return False
        
        return True

    async def _refresh_oauth_token(self, client: httpx.AsyncClient) -> Optional[str]:
        """
        Refresh OAuth token using refresh token from credentials file or sidecar cache.
        
        Calls platform.claude.com/v1/oauth/token endpoint with:
        - grant_type=refresh_token
        - refresh_token from ~/.claude/.credentials.json or sidecar cache
        - client_id (public identifier from Claude Code CLI)
        
        On success, updates credentials file with new tokens.
        On failure, implements exponential backoff or terminal failure.
        
        Args:
            client: httpx.AsyncClient for making requests
            
        Returns:
            Optional[str]: New access token if successful, None otherwise
        """
        if not self._can_attempt_refresh():
            return None
        
        # Load refresh token from credentials file or sidecar cache
        refresh_token = None
        
        # Priority 1: Credentials file (local access)
        try:
            if os.path.exists(self._credentials_path):
                with open(self._credentials_path, 'r') as f:
                    data = json.load(f)
                    refresh_token = data.get("claudeAiOauth", {}).get("refreshToken")
        except Exception as e:
            logger.warning(f"Could not load credentials for refresh: {e}")
        
        # Priority 2: Sidecar token cache (for multi-host scenarios)
        if not refresh_token:
            refresh_token = await token_cache.get_token("anthropic", "refresh_token")
            if refresh_token:
                logger.debug("Using refresh token from sidecar cache")
        
        if not refresh_token:
            logger.debug("No refresh token available in credentials file")
            return None
        
        try:
            logger.info("Attempting OAuth token refresh")
            
            resp = await client.post(
                "https://platform.claude.com/v1/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLAUDE_OAUTH_CLIENT_ID,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json"
                },
                timeout=30.0
            )
            
            if resp.status_code == 200:
                data = resp.json()
                new_access_token = data.get("access_token")
                new_refresh_token = data.get("refresh_token", refresh_token)
                raw_expires_in = data.get("expires_in")
                expires_in = int(raw_expires_in) if raw_expires_in is not None else 28800
                
                # Persist new tokens to credentials file
                self._persist_refreshed_tokens(
                    new_access_token, 
                    new_refresh_token, 
                    expires_in
                )
                
                # Update sidecar token cache so other sessions have the new token
                await token_cache.store("anthropic", {"oauth_token": new_access_token, "refresh_token": new_refresh_token})
                
                # Reset failure tracking
                self._last_refresh_failure = None
                self._refresh_backoff_seconds = 30
                self._terminal_failure = False
                
                logger.info(f"Token refresh successful, new token expires in {expires_in}s")
                return new_access_token
            
            # Handle failures
            error_data = resp.json() if resp.text else {}
            error_code = error_data.get("error", "")
            
            if resp.status_code in (400, 401):
                if error_code == "invalid_grant":
                    # Terminal failure - need to re-authenticate
                    self._terminal_failure = True
                    logger.error("Token refresh failed: invalid_grant - need to run 'claude login'")
                else:
                    # Transient failure - exponential backoff
                    self._last_refresh_failure = datetime.now(timezone.utc)
                    self._refresh_backoff_seconds = min(
                        self._refresh_backoff_seconds * 2,
                        self._max_refresh_backoff
                    )
                    logger.warning(f"Token refresh failed ({error_code}), backoff: {self._refresh_backoff_seconds}s")
            else:
                logger.error(f"Token refresh failed with status {resp.status_code}: {error_code}")
                
        except httpx.HTTPError as e:
            self._last_refresh_failure = datetime.now(timezone.utc)
            self._refresh_backoff_seconds = min(self._refresh_backoff_seconds * 2, self._max_refresh_backoff)
            logger.warning(f"Token refresh HTTP error: {e}, backoff: {self._refresh_backoff_seconds}s")
        except Exception as e:
            logger.error(f"Token refresh unexpected error: {e}")
        
        return None

    def _persist_refreshed_tokens(self, access_token: str, refresh_token: str, expires_in: int):
        """
        Persist refreshed tokens to ~/.claude/.credentials.json.
        """
        # Skip persistence in Docker mode (as per Mode 3 rule)
        if settings.RUN_MODE == "docker":
            logger.info("Skipping token persistence in Docker mode")
            return

        try:
            # Load existing credentials
            data = {}
            if os.path.exists(self._credentials_path):
                with open(self._credentials_path, 'r') as f:
                    data = json.load(f)
            
            # Update OAuth section
            if "claudeAiOauth" not in data:
                data["claudeAiOauth"] = {}
            
            # Ensure expires_in is a valid number
            expires_in_val = float(expires_in) if expires_in is not None else 28800.0
            expires_at_ms = int((time.time() + expires_in_val) * 1000)
            
            data["claudeAiOauth"]["accessToken"] = access_token
            data["claudeAiOauth"]["refreshToken"] = refresh_token
            data["claudeAiOauth"]["expiresAt"] = expires_at_ms
            
            # Write back
            with open(self._credentials_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.info(f"Persisted refreshed tokens to {self._credentials_path}")
            
        except Exception as e:
            logger.error(f"Failed to persist refreshed tokens: {e}")

    async def _get_claude_oauth_with_cache(self, client: httpx.AsyncClient, token: str):
        """
        Fetch Claude OAuth usage with caching and automatic token refresh.

        Checks token expiration before use and attempts automatic refresh if expired.
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

        # Check if token is expired and attempt refresh
        if self._is_token_expired(token):
            logger.info("OAuth token expired, attempting refresh")
            new_token = await self._refresh_oauth_token(client)
            if new_token:
                token = new_token
            else:
                logger.warning("Token refresh failed or unavailable, will try with current token")

        res = await self._get_claude_oauth(client, token)

        # Check if 401 (unauthorized) - try refreshing token once
        is_401 = any("Expired/Invalid Token" in r.get("detail", "") for r in res)
        if is_401 and not self._terminal_failure:
            logger.info("Got 401 from OAuth API, attempting token refresh")
            new_token = await self._refresh_oauth_token(client)
            if new_token:
                # Retry with new token
                res = await self._get_claude_oauth(client, new_token)

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
        headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
        
        # Mapping for human-friendly names
        name_map = {
            "five_hour": "Session Window",
            "seven_day": "Weekly Window",
            "seven_day_sonnet": "Sonnet Weekly",
            "seven_day_opus": "Opus Weekly",
            "extra_usage": "Extra Usage"
        }
        
        try:
            # Use retry logic for rate limit handling
            resp = await http_request_with_retry(client, "GET", url, headers=headers, timeout=10.0)
            
            if resp.status_code == 401: 
                return [error_card("Claude Pro", "🟠", "Expired/Invalid Token (OAuth)", error_type="auth_failed")]
            if resp.status_code == 429: 
                return [error_card("Claude Pro", "🟠", "Rate Limited (429) - max retries exceeded", error_type="rate_limited")]
            if resp.status_code != 200: 
                return [error_card("Claude Pro", "🟠", f"API Error {resp.status_code}", error_type="api_error")]
            
            data = resp.json()
            return self._parse_oauth_response(data, name_map)
            
        except Exception as e:
            logger.error(f"Claude OAuth collection failed: {e}")
            return [error_card("Claude Pro", "🟠", f"Conn Fail: {str(e)[:20]}", error_type="timeout")]

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

    def _parse_oauth_response(self, data: Dict[str, Any], name_map: Dict[str, str]) -> List[Dict[str, Any]]:
        """Parse OAuth API response into quota cards with null-safety."""
        results = []
        
        # Extract identity and tier once for all cards
        identity_str = self._extract_identity_from_oauth(data)
        identity_suffix = f" | {identity_str}" if identity_str else ""
        
        # Extract plan from account data for tier badge
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
        
        return results if results else [error_card("Claude Pro", "🟠", "No quota data", error_type="parse_error")]

    async def _get_claude_via_web_api(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
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
        session_key = get_claude_session_cookie()
        if not session_key:
            logger.debug("No Claude sessionKey cookie found in Chrome")
            return []
        
        headers = {"Cookie": f"sessionKey={session_key}"}

        try:
            # Step 1: Get organization ID
            orgs_resp = await client.get(
                "https://claude.ai/api/organizations",
                headers=headers,
                timeout=10.0
            )
            
            if orgs_resp.status_code != 200:
                logger.warning(f"Claude Web API orgs call failed: {orgs_resp.status_code}")
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
                    "https://claude.ai/api/account",
                    headers=headers,
                    timeout=10.0
                )
                if account_resp.status_code == 200:
                    account_data = account_resp.json()
            except Exception as e:
                logger.debug(f"Could not fetch account info: {e}")
            
            # Step 3: Get usage data
            usage_resp = await client.get(
                f"https://claude.ai/api/organizations/{org_id}/usage",
                headers=headers,
                timeout=10.0
            )

            if usage_resp.status_code != 200:
                logger.warning(f"Claude Web API usage call failed: {usage_resp.status_code}")
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

    def _parse_web_api_response(self, data: Dict[str, Any], org_data: Dict[str, Any] = None, account_data: Dict[str, Any] = None) -> List[Dict[str, Any]]:
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
            
            results.append({
                "service": f"Claude ({display_name})",
                "icon": "🟠",
                "remaining": f"{remaining_pct:.1f}%",
                "unit": "capacity",
                "reset": human_delta(reset_at),
                "health": "good" if pct_used < 70 else "warning" if pct_used < 90 else "critical",
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
            })
        
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
                results.append({
                    "service": "Claude (Extra Usage)",
                    "icon": "🟠",
                    "remaining": f"${remaining_pct:.0f}%",
                    "unit": "spend",
                    "reset": "Monthly",
                    "health": "good" if pct_used < 70 else "warning" if pct_used < 90 else "critical",
                    "pace": "Sustainable",
                    "detail": f"${spend:.2f} / ${limit:.2f} [Web API]{identity_suffix}",
                    "tier": tier,
                    "usage_url": "https://claude.ai/settings/usage",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
        
        return results

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
            return None

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
        limit = settings.CLAUDE_FREE_LIMIT if tier == "Free" else settings.CLAUDE_PRO_LIMIT
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

                        total_tokens += input_tokens + output_tokens + cache_read + cache_creation

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

        return [{
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
        }]

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
                    projects_path = os.path.join(path, "projects") if not path.endswith("/projects") else path
                    if os.path.isdir(projects_path):
                        dirs.append(projects_path)
        
        # Priority 2: Default locations (platform-aware)
        default_paths = [
            os.path.join(get_platform_config_dir("claude"), "projects"),
            os.path.expanduser("~/.config/claude/projects"),  # Legacy/Generic Linux
            os.path.expanduser("~/.claude/projects"),          # Legacy/Direct home
        ]
        
        for path in default_paths:
            if os.path.isdir(path) and path not in dirs:
                dirs.append(path)
        
        return dirs
