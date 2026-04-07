"""
Anthropic (Claude) quota collector with 3-tier fallback strategy.

Collection Strategy:
1. Primary: OAuth API endpoint (https://api.anthropic.com/api/oauth/usage)
   - Requires CLAUDE_CODE_OAUTH_TOKEN environment variable
   - Returns real-time usage across multiple quota windows (5h, 7d, etc.)
   - Implements caching (10 min TTL) to avoid rate limiting
   
2. Secondary: Local log parsing from Claude Projects
   - Parses .jsonl files from ~/.claude/projects/ directory
   - Calculates token usage from assistant messages (input + output tokens)
   - Uses 5-hour sliding window to match OAuth behavior
   
3. Tertiary: Error cards when both methods fail
   - Returns descriptive error if token expired/invalid
   - Falls back to local logs when rate limited (429)
   - Final fallback shows "No data" if logs unavailable

Data Caching:
- OAuth results cached for 10 minutes to handle 429 rate limits gracefully
- Cached results tagged with "[Cached]" in detail field
- Falls back to local logs without repeating failed API calls

Error Handling:
- 401: Token expired/invalid (prompt re-authentication)
- 429: Rate limited (use cache if available, fall back to logs)
- Other errors: Show connection error with first 20 chars of message
"""

import os
import glob
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import httpx
from app.core.config import settings
from app.core.utils import PaceCalculator, human_delta, error_card, http_request_with_retry
from app.services.collectors.base import BaseCollector

logger = logging.getLogger(__name__)

class AnthropicCollector(BaseCollector):
    """Collector for Anthropic (Claude) quota and usage metrics."""
    
    def __init__(self):
        """Initialize caching for OAuth results to avoid rate limiting."""
        self._cached_results = None
        self._last_fetch = None
        self._cache_ttl = 600  # 10 minutes cache to be safe with 429s

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Claude quota data using 3-tier fallback strategy.
        
        Tries in order:
        1. OAuth API with caching (if CLAUDE_CODE_OAUTH_TOKEN set)
        2. Local log parsing (if OAuth fails or unavailable)
        3. Return original OAuth error or generic "no data" error
        
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
            
            # If it was a 429 specifically, we might want to log it or just proceed to fallback
            # The error_card detail will contain "API Error 429"

        # 2. Fallback to Local Logs
        local_res = await self._get_claude_local()
        if local_res:
            # If we fell back due to an error, we could tag it
            if settings.CLAUDE_CODE_OAUTH_TOKEN:
                for r in local_res:
                    r["detail"] += " (API Fallback)"
            return local_res
            
        # 3. Final Fallback: Return the original OAuth error if both failed
        if settings.CLAUDE_CODE_OAUTH_TOKEN:
            return await self._get_claude_oauth(client, settings.CLAUDE_CODE_OAUTH_TOKEN)
            
        return [error_card("Claude Pro", "🟠", "No data — OAuth missing & Logs empty")]

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
                    if "[Cached]" not in r["detail"]:
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
            results = []
            
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
                    "detail": f"{pct_used:.1f}% used [OAuth]",
                })
            return results if results else [error_card("Claude Pro", "🟠", "No quota data")]
        except Exception as e:
            logger.error(f"Claude OAuth collection failed: {e}")
            return [error_card("Claude Pro", "🟠", f"Conn Fail: {str(e)[:20]}")]

    async def _get_claude_local(self):
        """
        Fallback: Parse Claude usage from local project logs.
        
        Scans ~/.claude/projects/**/*.jsonl files for assistant message logs
        within the last 5 hours. Sums input_tokens + output_tokens to estimate
        usage against the 2M token/5h quota window.
        
        Uses:
        - As fallback when OAuth API is unavailable or rate limited
        - For monitoring local Claude usage (e.g., in development)
        
        Data Source:
        - Location: Configured by CLAUDE_PROJECTS_DIR (default: ~/.claude/projects)
        - Format: JSONL files with entries: {"type": "assistant", "timestamp": "...", "message": {"usage": {...}}}
        - Time Window: Last 5 hours of assistant messages only
        
        Returns:
            List[Dict[str, Any]]: Single card with total tokens or None if logs unavailable
        """
        projects_dir = settings.CLAUDE_PROJECTS_DIR
        limit = 2000000
        try:
            files = glob.glob(f"{projects_dir}/**/*.jsonl", recursive=True)
            if not files: return None
            cutoff = datetime.now(timezone.utc) - timedelta(hours=5)
            total_tokens = 0
            oldest: Optional[datetime] = None
            for fpath in files:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line)
                        if entry.get("type") != "assistant": continue
                        ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                        if ts < cutoff: continue
                        usage = entry.get("message", {}).get("usage", {})
                        total_tokens += (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
                        if not oldest or ts < oldest: oldest = ts
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
                 "detail": f"{total_tokens:,} / {limit:,} [Logs]",
             }]
        except FileNotFoundError:
            logger.debug(f"Claude projects directory not found: {projects_dir}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in Claude logs: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to parse Claude logs: {e}")
            return None
