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

class ChatGPTCollector(BaseCollector):
    async def _get_auth_data(self) -> Dict[str, Any]:
        """
        Retrieve ChatGPT authentication token from environment or local cache.
        
        Tries in priority order:
        1. CHATGPT_OAUTH_TOKEN environment variable
        2. ~/.codex/auth.json (Codex CLI auth cache)
        
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
            except: pass
        return {}

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect ChatGPT Codex quota using API with local cache fallback.
        
        Attempts:
        1. API call to wham/usage if token available
        2. Falls back to local session cache if API fails or no token
        3. Returns error card if both fail
        
        Returns:
            List[Dict[str, Any]]: Single card with usage percentage or error
        """
        auth = await self._get_auth_data()
        token = auth.get("token")
        
        if token:
            try:
                # Internal wham/usage endpoint (as used by CodexBar/CLI)
                # Note: This is an unauthenticated-looking but actually Bearer-auth'd endpoint
                url = "https://chatgpt.com/backend-api/wham/usage"
                headers = {"Authorization": f"Bearer {token}"}
                resp = await client.get(url, headers=headers, timeout=5)
                
                if resp.status_code == 200:
                    data = resp.json()
                    # Expecting primary/secondary usage windows
                    primary = data.get("primary", {})
                    pct = primary.get("utilization_percent", 0.0)
                    reset_ts = primary.get("resets_at")
                    reset_at = datetime.fromtimestamp(reset_ts, tz=timezone.utc) if reset_ts else None
                    
                    return [{
                        "service": "ChatGPT Codex",
                        "icon": "💬",
                        "remaining": f"{(100-pct):.1f}%",
                        "unit": "remaining",
                        "reset": human_delta(reset_at),
                        "health": "good" if pct < 80 else "warning",
                        "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                        "detail": "API: wham/usage"
                    }]
            except Exception as e:
                # Fallback to local logs on API failure
                pass

        # Local log fallback (original logic)
        path = settings.CHATGPT_SESSIONS_DIR
        try:
            files = glob.glob(f"{path}/**/*.jsonl", recursive=True)
            if not files: 
                # If no logs but we have a token that failed, report error
                if token: return [error_card("ChatGPT Codex", "💬", "API Error")]
                return [error_card("ChatGPT Codex", "💬", "No logs/auth")]
                
            latest = max(files, key=os.path.getmtime)
            with open(latest, "r") as f:
                lines = f.readlines()
                if not lines: return [error_card("ChatGPT Codex", "💬", "Empty log")]
                usage = json.loads(lines[-1])
                
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
                "detail": f"{pct:.1f}% used [Cache]",
            }]
        except: return [error_card("ChatGPT Codex", "💬", "Parse Error")]
