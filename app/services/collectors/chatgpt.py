"""
ChatGPT Codex quota collector with API and local cache fallback.

Collection Strategy:
1. Primary: ChatGPT wham/usage API endpoint
   - Requires OAuth token from environment (CHATGPT_OAUTH_TOKEN) or ~/.codex/auth.json
   - Calls https://chatgpt.com/backend-api/wham/usage (requires Bearer auth)
   - Returns utilization percentage and reset timestamp
   
2. Token Priority:
   - Priority 1: CHATGPT_OAUTH_TOKEN environment variable (if set)
   - Priority 2: ~/.codex/auth.json (Codex CLI cache location)
   
3. Fallback: Local session cache
   - Parses CHATGPT_SESSIONS_DIR for .jsonl session files
   - Uses most recently modified file (represents latest session)
   - Reads last line of log file for cached usage snapshot
   - Falls back if API fails with cached data from last known state
   
4. Error Handling:
   - No auth: Returns "No logs/auth" error
   - API failure: Falls back to local logs
   - Empty/invalid logs: Returns parse error card

Timestamp Handling:
- API returns Unix timestamps in seconds (resets_at field)
- Converted to UTC datetime for human-readable reset display
"""

import os
import glob
import json
from datetime import datetime, timezone
from typing import List, Dict, Any
import httpx
from app.core.config import settings
from app.core.utils import PaceCalculator, human_delta, error_card
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

class ChatGPTCollector(BaseCollector):
    def __init__(self):
        """Initialize caching for API results."""
        self._cached_api_results = None
        self._last_api_fetch = None
        self._cache_ttl = 300  # 5 minutes cache for lighter rate limits

    def _is_error_result(self, results: List[Dict[str, Any]]) -> bool:
        """Check if results contain an error card."""
        if not results:
            return True
        return any(r.get("remaining") == "ERR" for r in results)

    async def _get_auth_data(self) -> Dict[str, Any]:
        """
        Retrieve ChatGPT authentication token from environment or local cache.
        
        Tries in priority order:
        1. CHATGPT_OAUTH_TOKEN environment variable
        2. ~/.codex/auth.json (Codex CLI auth cache)
        3. Token cache from sidecar
        
        Returns:
            Dict with "token" and optionally "path" keys, or empty dict if not found
        """
        # Priority 1: Env var
        token = os.getenv("CHATGPT_OAUTH_TOKEN", "")
        if token: return {"token": token}
        
        # Priority 2: ~/.codex/auth.json
        auth_path = os.path.expanduser("~/.codex/auth.json")
        if os.path.exists(auth_path):
            try:
                with open(auth_path, "r") as f:
                    data = json.load(f)
                    token = data.get("tokens", {}).get("access_token")
                    if token: return {"token": token, "path": auth_path}
            except (IOError, json.JSONDecodeError):
                pass
            
        # Check token cache from sidecar
        if not token:
            token = await token_cache.get_token("chatgpt", "oauth_token")
            if token:
                logger.debug("Using OAuth token from sidecar cache")
                return {"token": token, "path": "cache"}
            
        return {}

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect ChatGPT Codex quota using API with caching and local fallback.

        Attempts:
        1. Check API cache (5 min TTL) - use if fresh
        2. API call to wham/usage if token available (cache result)
        3. Falls back to local session cache if API fails or cached error
        4. Returns error card if both fail

        Returns:
            List[Dict[str, Any]]: Cards with usage percentage or error
        """
        auth = await self._get_auth_data()
        token = auth.get("token")

        # Check API cache first (check is not None for empty lists)
        now = datetime.now(timezone.utc)
        cached_error = None
        if self._cached_api_results is not None and self._last_api_fetch:
            if (now - self._last_api_fetch).total_seconds() < self._cache_ttl:
                # Return cached success result immediately
                if not self._is_error_result(self._cached_api_results):
                    return self._cached_api_results
                # Cached error - save it for potential return later, skip API call
                # (don't hammer the API with repeated error results)
                cached_error = self._cached_api_results

        # Try API if we have a token and no cached error
        if token and not cached_error:
            try:
                # Internal wham/usage endpoint (as used by CodexBar/CLI)
                url = "https://chatgpt.com/backend-api/wham/usage"
                headers = {"Authorization": f"Bearer {token}"}
                resp = await client.get(url, headers=headers, timeout=5)

                if resp.status_code == 200:
                    data = resp.json()
                    # Extract tier from plan_type
                    plan_type = data.get("plan_type", "unknown")
                    tier = plan_type.lower() if plan_type != "unknown" else None

                    cards = []

                    # Main rate limit (primary_window)
                    rate_limit = data.get("rate_limit", {})
                    primary = rate_limit.get("primary_window", {})
                    if primary:
                        pct = primary.get("used_percent", 0.0)
                        reset_ts = primary.get("reset_at")
                        reset_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc) if reset_ts else None

                        cards.append({
                            "service": "ChatGPT Codex",
                            "icon": "💬",
                            "remaining": f"{(100-pct):.1f}%",
                            "unit": "remaining",
                            "reset": human_delta(reset_at),
                            "health": "good" if pct < 80 else "warning",
                            "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                            "detail": f"{pct:.1f}% used",
                            "used_value": float(pct),
                            "limit_value": 100.0,
                            "unit_type": "percent",
                            "reset_at": reset_at.isoformat() if reset_at else None,
                            "data_source": "oauth",
                            "tier": tier,
                            "usage_url": "https://chatgpt.com/codex/settings/usage/",
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        })

                    # Code review rate limit (if available and different)
                    code_review = data.get("code_review_rate_limit", {})
                    cr_primary = code_review.get("primary_window", {})
                    if cr_primary and cr_primary != primary:
                        cr_pct = cr_primary.get("used_percent", 0.0)
                        cr_reset_ts = cr_primary.get("reset_at")
                        cr_reset_at = datetime.fromtimestamp(cr_reset_ts, tz=timezone.utc) if cr_reset_ts else None

                        cards.append({
                            "service": "ChatGPT Code Review",
                            "icon": "💬",
                            "remaining": f"{(100-cr_pct):.1f}%",
                            "unit": "remaining",
                            "reset": human_delta(cr_reset_at),
                            "health": "good" if cr_pct < 80 else "warning",
                            "pace": PaceCalculator.estimate_longevity(cr_pct, cr_reset_at),
                            "detail": f"{cr_pct:.1f}% used",
                            "used_value": float(cr_pct),
                            "limit_value": 100.0,
                            "unit_type": "percent",
                            "reset_at": cr_reset_at.isoformat() if cr_reset_at else None,
                            "data_source": "oauth",
                            "tier": tier,
                            "usage_url": "https://chatgpt.com/codex/settings/usage/",
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        })

                    # Cache successful API result
                    self._cached_api_results = cards
                    self._last_api_fetch = now

                    return cards if cards else [error_card("ChatGPT Codex", "💬", "No quota data", error_type="api_error")]

                else:
                    # API returned non-200 - cache error and fallback
                    error_result = [error_card("ChatGPT Codex", "💬", f"API Error {resp.status_code}", error_type="api_error")]
                    self._cached_api_results = error_result
                    self._last_api_fetch = now

            except Exception as e:
                # Cache exception as error
                error_result = [error_card("ChatGPT Codex", "💬", f"API Error: {str(e)[:20]}", error_type="api_error")]
                self._cached_api_results = error_result
                self._last_api_fetch = now

        # Fallback to local logs on API failure or cached error

        # Local log fallback (original logic)
        path = settings.CHATGPT_SESSIONS_DIR
        try:
            files = glob.glob(f"{path}/**/*.jsonl", recursive=True)
            if not files:
                # If no logs but we have a token that failed, return cached error if available
                if cached_error:
                    return cached_error
                if token:
                    return [error_card("ChatGPT Codex", "💬", "API Error", error_type="api_error")]
                return [error_card("ChatGPT Codex", "💬", "No logs/auth", error_type="missing_config")]
                
            latest = max(files, key=os.path.getmtime)
            last_line = None
            with open(latest, "r") as f:
                for line in f:
                    if line.strip():
                        last_line = line
            
            if not last_line:
                return [error_card("ChatGPT Codex", "💬", "Empty log", error_type="parse_error")]
            
            usage = json.loads(last_line)
                
            pct = usage.get("used_percent", 0.0)
            reset_at = datetime.fromtimestamp(usage["resets_at"], tz=timezone.utc) if "resets_at" in usage else None
            
            return [{
                "service": "ChatGPT Codex",
                "icon": "💬",
                "remaining": f"{(100-pct):.1f}%",
                "unit": "remaining",
                "reset": human_delta(reset_at),
                "health": "good" if pct < 80 else "warning",
                "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                "detail": f"{pct:.1f}% used",
                "used_value": float(pct),
                "limit_value": 100.0,
                "unit_type": "percent",
                "reset_at": reset_at.isoformat() if reset_at else None,
                "data_source": "cache",
                "usage_url": "https://chatgpt.com/codex/settings/usage/",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            return [error_card("ChatGPT Codex", "💬", "Parse Error", error_type="parse_error")]
