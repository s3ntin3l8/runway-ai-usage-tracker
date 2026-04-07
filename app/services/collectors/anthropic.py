"""
Anthropic (Claude) quota collector with 4-tier fallback strategy.

Collection Strategy:
1. Primary: OAuth API endpoint (https://api.anthropic.com/api/oauth/usage)
   - Requires CLAUDE_CODE_OAUTH_TOKEN environment variable or ~/.claude/.credentials.json
   - Returns real-time usage across multiple quota windows (5h, 7d, 7d-sonnet, 7d-opus, extra)
   - Implements caching (10 min TTL) to avoid rate limiting
   
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

Error Handling:
- 401: Token expired/invalid (prompt re-authentication)
- 429: Rate limited (use cache if available, fall back to Web API/logs)
- Connection errors: Fall back to next available method
- Missing files/logs: Return error card with helpful message
"""

import os
import glob
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
import httpx
from app.core.config import settings
from app.core.utils import PaceCalculator, human_delta, error_card, http_request_with_retry
from app.core.chrome_cookies import get_claude_session_cookie
from app.services.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


class AnthropicCollector(BaseCollector):
    """Collector for Anthropic (Claude) quota and usage metrics with 4-tier fallback."""
    
    def __init__(self):
        """Initialize caching for OAuth results to avoid rate limiting."""
        self._cached_results = None
        self._last_fetch = None
        self._cache_ttl = 600  # 10 minutes cache to be safe with 429s

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Claude quota data using 4-tier fallback strategy.
        
        Tries in order:
        1. OAuth API with caching (if CLAUDE_CODE_OAUTH_TOKEN set)
        2. Web API via Chrome cookies (if user logged into claude.ai)
        3. Enhanced local log parsing (multiple config roots, full token tracking)
        4. Return descriptive error if all methods fail
        
        Returns:
            List[Dict[str, Any]]: List of quota cards for each quota window.
        """
        # 1. Try OAuth if token exists
        if settings.CLAUDE_CODE_OAUTH_TOKEN:
            oauth_res = await self._get_claude_oauth_with_cache(client, settings.CLAUDE_CODE_OAUTH_TOKEN)
            
            # Check if it's a valid result (not an error card)
            is_error = any(r.get("remaining") == "ERR" for r in oauth_res)
            if not is_error and oauth_res:
                return oauth_res
            
            # Log OAuth failure for debugging
            logger.debug(f"OAuth failed, falling back to Web API. Result: {oauth_res}")

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
            return [error_card("Claude Pro", "🟠", "No data — OAuth failed & Logs empty")]
        
        if await self._has_web_cookie():
            return [error_card("Claude Pro", "🟠", "No data — Web API failed & Logs empty")]
            
        return [error_card("Claude Pro", "🟠", "No data — Set CLAUDE_CODE_OAUTH_TOKEN or login to claude.ai")]

    async def _has_web_cookie(self) -> bool:
        """Check if a web cookie is available without making API calls."""
        return get_claude_session_cookie() is not None

    async def _get_claude_oauth_with_cache(self, client: httpx.AsyncClient, token: str):
        """
        Fetch Claude OAuth usage with caching to handle rate limits.
        
        Caches results for 10 minutes. If cache is fresh, returns cached results
        tagged with "[Cached]" in the detail field. Otherwise fetches fresh data.
        
        Args:
            client: httpx.AsyncClient for making requests
            token: OAuth token for Anthropic API
            
        Returns:
            List[Dict[str, Any]]: Quota cards or error card if fetch fails
        """
        now = datetime.now(timezone.utc)
        if self._cached_results and self._last_fetch:
            if (now - self._last_fetch).total_seconds() < self._cache_ttl:
                # Add a tag to show it's cached
                for r in self._cached_results:
                    if "[Cached]" not in r.get("detail", ""):
                        r["detail"] += " [Cached]"
                return self._cached_results

        res = await self._get_claude_oauth(client, token)
        
        # Only cache if not an error
        is_error = any(r.get("remaining") == "ERR" for r in res)
        if not is_error and res:
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
                return [error_card("Claude Pro", "🟠", "Expired/Invalid Token (OAuth)")]
            if resp.status_code == 429: 
                return [error_card("Claude Pro", "🟠", "Rate Limited (429) - max retries exceeded")]
            if resp.status_code != 200: 
                return [error_card("Claude Pro", "🟠", f"API Error {resp.status_code}")]
            
            data = resp.json()
            return self._parse_oauth_response(data, name_map)
            
        except Exception as e:
            logger.error(f"Claude OAuth collection failed: {e}")
            return [error_card("Claude Pro", "🟠", f"Conn Fail: {str(e)[:20]}")]

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
        """Parse OAuth API response into quota cards."""
        results = []
        
        # Extract identity once for all cards
        identity_str = self._extract_identity_from_oauth(data)
        identity_suffix = f" | {identity_str}" if identity_str else ""
        
        # Sort by name_map order to keep it consistent
        sorted_keys = sorted(data.keys(), key=lambda k: list(name_map.keys()).index(k) if k in name_map else 999)
        
        for key in sorted_keys:
            usage = data[key]
            if not isinstance(usage, dict) or "utilization" not in usage:
                continue
            
            u_type = name_map.get(key, key.replace("_", " ").title())
            pct_used = usage.get("utilization", 0.0)
            remaining_pct = 100.0 - pct_used
            
            reset_raw = usage.get("resets_at") or usage.get("resetsAt")
            reset_at = None
            if reset_raw:
                try:
                    reset_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                except ValueError as e:
                    logger.warning(f"Failed to parse reset time: {reset_raw}, error: {e}")
            
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
            })
        
        return results if results else [error_card("Claude Pro", "🟠", "No quota data")]

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
            
            # Step 2: Get usage data
            usage_resp = await client.get(
                f"https://claude.ai/api/organizations/{org_id}/usage",
                headers=headers,
                timeout=10.0
            )

            if usage_resp.status_code != 200:
                logger.warning(f"Claude Web API usage call failed: {usage_resp.status_code}")
                return []

            usage_data = usage_resp.json()
            return self._parse_web_api_response(usage_data, org)
            
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

    def _parse_web_api_response(self, data: Dict[str, Any], org_data: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Parse Web API response into quota cards."""
        results = []
        
        # Extract identity once for all cards
        identity_str = self._extract_identity_from_web(org_data) if org_data else ""
        identity_suffix = f" | {identity_str}" if identity_str else ""
        
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
            
            # Get usage percentage
            pct_used = window_data.get("percentUsed", 0.0)
            remaining_pct = 100.0 - pct_used
            
            # Parse reset time
            reset_at = None
            reset_raw = window_data.get("resetsAt")
            if reset_raw:
                try:
                    reset_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                except ValueError:
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
            })
        
        # Add extra usage if present
        extra_data = data.get("extra_usage") or data.get("overage")
        if extra_data and isinstance(extra_data, dict):
            spend = extra_data.get("spend", 0)
            limit = extra_data.get("limit", 0)
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
                })
        
        return results

    async def _get_claude_local_enhanced(self) -> List[Dict[str, Any]]:
        """
        Enhanced fallback: Parse Claude usage from local project logs.
        
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
        
        # 5-hour window to match OAuth session window
        limit = 2000000  # 2M tokens per 5h window (Pro tier)
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
            "unit_type": "tokens",
            "reset_at": reset_at.isoformat() if reset_at else None,
            "data_source": "local",
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
        
        # Priority 2: Default locations
        default_paths = [
            os.path.expanduser("~/.config/claude/projects"),
            os.path.expanduser("~/.claude/projects"),
        ]
        
        for path in default_paths:
            if os.path.isdir(path) and path not in dirs:
                dirs.append(path)
        
        return dirs
