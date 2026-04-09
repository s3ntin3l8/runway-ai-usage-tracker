"""
ChatGPT Codex quota collector with API and local cache fallback.

Collection Strategy:
1. Primary: ChatGPT wham/usage API endpoint
   - Requires OAuth token from environment (CHATGPT_OAUTH_TOKEN) or ~/.codex/auth.json
   - Falls back to Web Dashboard Scraping (Browser Cookies) if no token is found
   - Calls https://chatgpt.com/backend-api/wham/usage (requires Bearer auth)
   - Returns utilization percentage and reset timestamp
   
2. Authentication Priority:
   - Priority 1: CHATGPT_OAUTH_TOKEN environment variable
   - Priority 2: ~/.codex/auth.json (Codex CLI auth cache)
   - Priority 3: Browser Cookies (__Secure-next-auth.session-token)
   - Priority 4: Sidecar cache (forwarded tokens/cookies from other hosts)
   
3. Fallback: Local session cache
   - Parses CHATGPT_SESSIONS_DIR for .jsonl session files
   - Uses most recently modified file (represents latest session)
   - Reads last line of log file for cached usage snapshot
   
4. Error Handling:
   - No auth: Returns "No logs/auth" error
   - API failure: Falls back to local logs
   - Empty/invalid logs: Returns parse error card
"""

import os
import glob
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import asyncio
import httpx
from app.core.config import settings
from app.core.utils import PaceCalculator, human_delta, error_card
from app.core.browser_cookies import get_chatgpt_session_token
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

class ChatGPTCollector(BaseCollector):
    def __init__(self):
        """Initialize caching for API results and refreshed tokens."""
        self._cached_api_results = None
        self._last_api_fetch = None
        self._cache_ttl = 300  # 5 minutes cache for lighter rate limits
        
        # Token refresh cache
        self._refreshed_token = None
        self._refreshed_token_expiry = None

    def _is_error_result(self, results: List[Dict[str, Any]]) -> bool:
        """Check if results contain an error card."""
        if not results:
            return True
        return any(r.get("remaining") == "ERR" for r in results)

    async def _get_auth_data(self, client: httpx.AsyncClient) -> Dict[str, Any]:
        """
        Retrieve ChatGPT auth with priority: OAUTH -> Browser Cookies -> Sidecar Cache.
        """
        # Priority 1: Env var
        token = os.getenv("CHATGPT_OAUTH_TOKEN", "")
        if token: return {"token": token, "source": "env"}
        
        # Priority 2: ~/.codex/auth.json
        auth_path = settings.CHATGPT_AUTH_PATH
        if os.path.exists(auth_path):
            try:
                with open(auth_path, "r") as f:
                    data = json.load(f)
                    token = data.get("tokens", {}).get("access_token")
                    if token: return {"token": token, "source": "auth_json"}
            except (IOError, json.JSONDecodeError):
                pass

        # Priority 3: Browser Cookies
        session_token = get_chatgpt_session_token()
        if not session_token:
            # Check sidecar cache for cookie
            session_token = await token_cache.get_token("chatgpt", "cookie___Secure-next-auth.session-token")
        
        if session_token:
            # Try to get refreshed token from in-memory cache
            now = datetime.now(timezone.utc)
            if self._refreshed_token and self._refreshed_token_expiry and now < self._refreshed_token_expiry:
                return {"token": self._refreshed_token, "source": "cookies_cached"}
            
            # Refresh Bearer token using session cookie
            refreshed = await self._refresh_access_token(client, session_token)
            if refreshed:
                self._refreshed_token = refreshed
                self._refreshed_token_expiry = now + timedelta(hours=1)
                return {"token": refreshed, "source": "cookies"}

        # Priority 4: Sidecar cache (direct OAuth token)
        token = await token_cache.get_token("chatgpt", "oauth_token")
        if token:
            logger.debug("Using OAuth token from sidecar cache")
            return {"token": token, "source": "sidecar_cache"}
            
        return {}

    async def _refresh_access_token(self, client: httpx.AsyncClient, session_token: str) -> Optional[str]:
        """Exchange session cookie for a Bearer accessToken."""
        try:
            url = "https://chatgpt.com/api/auth/session"
            headers = {
                "Cookie": f"__Secure-next-auth.session-token={session_token}",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = await client.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("accessToken")
            else:
                logger.debug(f"Failed to refresh ChatGPT token: HTTP {resp.status_code}")
        except Exception as e:
            logger.debug(f"Error refreshing ChatGPT token: {e}")
        return None

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect ChatGPT Codex quota and Account info using API with caching and local fallback.
        """
        auth = await self._get_auth_data(client)
        token = auth.get("token")

        # Check API cache first
        now = datetime.now(timezone.utc)
        cached_error = None
        if self._cached_api_results is not None and self._last_api_fetch:
            if (now - self._last_api_fetch).total_seconds() < self._cache_ttl:
                if not self._is_error_result(self._cached_api_results):
                    return self._cached_api_results
                cached_error = self._cached_api_results

        # Try API if we have a token and no fresh cached error
        # (We skip API if we have a cached error that is very recent, e.g. < 60s)
        if token:
            skip_api = False
            if cached_error:
                error_age = (now - self._last_api_fetch).total_seconds()
                if error_age < 60: # Only skip API for 60s on error
                    skip_api = True
            
            if not skip_api:
                try:
                    headers = {"Authorization": f"Bearer {token}"}
                    cards = []
                    
                    # 1. Fetch Account Info (Tier, Credits)
                    account_url = "https://chatgpt.com/backend-api/accounts/check/v4"
                    acc_resp = await client.get(account_url, headers=headers, timeout=5)
                    acc_resp.raise_for_status()
                    
                    acc_data = acc_resp.json()
                    account_list = acc_data.get("accounts", {})
                    primary_account = next(iter(account_list.values()), {}) if account_list else {}
                    entitlements = primary_account.get("entitlements", [])
                    
                    if any(e.get("slug") == "plus" for e in entitlements):
                        tier = "plus"
                    elif any(e.get("slug") == "team" for e in entitlements):
                        tier = "team"
                    else:
                        tier = "free"

                    status = primary_account.get("account_status", "active")
                    cards.append({
                        "service": "ChatGPT Account",
                        "icon": "💬",
                        "remaining": tier.upper(),
                        "unit": "tier",
                        "reset": status.capitalize(),
                        "health": "good",
                        "pace": "Active" if status == "active" else "Alert",
                        "detail": f"Account Tier: {tier.capitalize()} · {status}",
                        "data_source": auth.get("source", "oauth"),
                        "tier": tier,
                        "updated_at": now.isoformat(),
                    })

                    # 2. Fetch Usage (wham/usage)
                    usage_url = "https://chatgpt.com/backend-api/wham/usage"
                    resp = await client.get(usage_url, headers=headers, timeout=5)
                    resp.raise_for_status()
                    
                    data = resp.json()
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
                            "data_source": auth.get("source", "oauth"),
                            "tier": tier,
                            "usage_url": "https://chatgpt.com/codex/settings/usage/",
                            "updated_at": now.isoformat(),
                        })

                    if cards:
                        self._cached_api_results = cards
                        self._last_api_fetch = now
                        return cards

                except Exception as e:
                    logger.debug(f"ChatGPT API Error: {e}")
                    error_result = [error_card("ChatGPT Codex", "💬", f"API Error: {str(e)[:20]}", error_type="api_error")]
                    self._cached_api_results = error_result
                    self._last_api_fetch = now
                    cached_error = error_result # Update local cached_error for potential returns

        # Fallback to local logs
        path = settings.CHATGPT_SESSIONS_DIR
        try:
            # Wrap blocking glob and stat calls in thread
            files = await asyncio.to_thread(glob.glob, f"{path}/**/*.jsonl", recursive=True)
            if not files:
                # If we have an API error (either from this call or cached), prefer returning that
                if cached_error: return cached_error
                if token: return [error_card("ChatGPT Codex", "💬", "API Error", error_type="api_error")]
                return [error_card("ChatGPT Codex", "💬", "No logs/auth", error_type="missing_config")]
                
            latest = await asyncio.to_thread(max, files, key=os.path.getmtime)
            
            def read_last_line(file_path):
                last_line = None
                with open(file_path, "r") as f:
                    for line in f:
                        if line.strip(): last_line = line
                return last_line

            last_line = await asyncio.to_thread(read_last_line, latest)
            
            if not last_line: return [error_card("ChatGPT Codex", "💬", "Empty log", error_type="parse_error")]
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
        except Exception as e:
            logger.debug(f"ChatGPT Fallback Error: {e}")
            return [error_card("ChatGPT Codex", "💬", "Parse Error", error_type="parse_error")]
