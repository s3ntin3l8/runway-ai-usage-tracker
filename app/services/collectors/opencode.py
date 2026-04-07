"""
OpenCode quota collector with web API (Chrome cookies) as primary source.

Collection Strategy:
1. OpenCode Web API (PRIMARY)
   - Uses Chrome cookies to authenticate with opencode.ai
   - Calls https://opencode.ai/_server endpoint
   - Returns aggregated usage from ALL devices (web IDE, TUI, etc.)
   - Shows rolling 5-hour and weekly windows
   
2. Sidecar Aggregation (FALLBACK)
   - Aggregates local DB data from multiple hosts via external metrics
   - Used when web API fails (no Chrome login, cookie decryption fails)
   - Each host runs sidecar script to push local data

Local DB Collection:
- Controlled by OPENCODE_LOCAL_COLLECTOR_ENABLED env var
- Only used as additional data source, not primary
"""

import os
import re
from typing import List, Dict, Any, Optional
import httpx
from app.core.config import settings
from app.core.utils import error_card, human_delta
from app.core.chrome_cookies import get_opencode_session_cookie
from app.services.collectors.base import BaseCollector
from app.services.external_metrics import external_metric_service
from app.services.token_cache import token_cache


class OpenCodeCollector(BaseCollector):
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect OpenCode quota from web API (primary) or sidecar aggregation (fallback).
        
        Priority:
        1. Web API with Chrome cookies - shows total account usage across all devices
        2. Sidecar aggregation - combines local DB data from multiple hosts
        
        Returns:
            List[Dict[str, Any]]: Cards for 5h and weekly windows
        """
        # 1. Try web API first (aggregates all devices via opencode.ai account)
        web_cards = await self._get_opencode_web(client)
        if web_cards:
            return web_cards
        
        # 2. Fall back to sidecar aggregation
        sidecar_cards = external_metric_service.get_opencode_aggregated()
        if sidecar_cards:
            return sidecar_cards
        
        # 3. Last resort: local DB (if enabled)
        if os.getenv("OPENCODE_LOCAL_COLLECTOR_ENABLED", "true").lower() != "false":
            return await self._get_opencode_tui()
        
        return []

    async def _get_opencode_web(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Fetch OpenCode usage from web API using Chrome cookies.
        
        This queries the opencode.ai servers and returns aggregated usage
        from ALL devices where the user is logged in (web IDE, TUI, etc.).
        
        Process:
        1. Extract session cookie from Chrome
        2. Call workspaces endpoint to get workspace ID
        3. Call subscription endpoint to get usage data
        4. Parse JavaScript response with regex
        
        Returns:
            List[Dict[str, Any]]: Cards for 5h and weekly windows, or empty list on failure
        """
        # Check for session cookie (local Chrome or sidecar cache)
        session_cookie = get_opencode_session_cookie()
        cookie_source = "local"
        
        if not session_cookie:
            session_cookie = token_cache.get_token("opencode", "cookie_session")
            if session_cookie:
                cookie_source = "sidecar"
                logger.info("Using OpenCode session cookie from sidecar cache")
        
        if not session_cookie:
            return []
        
        try:
            headers = {
                "Cookie": f"session={session_cookie}",
                "Content-Type": "application/json",
            }
            
            # 1. Get workspace ID
            workspace_id = await self._get_workspace_id(client, headers)
            if not workspace_id:
                return []
            
            # 2. Get subscription data
            usage_data = await self._get_subscription_data(client, headers, workspace_id)
            if not usage_data:
                return []
            
            # 3. Parse and return cards
            return self._parse_usage_data(usage_data)
            
        except Exception:
            return []

    async def _get_workspace_id(
        self, 
        client: httpx.AsyncClient, 
        headers: Dict[str, str]
    ) -> Optional[str]:
        """Get the first workspace ID from opencode.ai."""
        try:
            # Check for env override first
            env_workspace = os.getenv("OPENCODE_WORKSPACE_ID")
            if env_workspace:
                # Handle full URL format
                if "workspace/" in env_workspace:
                    return env_workspace.split("workspace/")[-1].split("/")[0]
                return env_workspace
            
            # Call workspaces endpoint
            resp = await client.post(
                "https://opencode.ai/_server",
                headers=headers,
                json={
                    "functionId": "def39973159c7f0483d8793a822b8dbb10d067e12c65455fcb4608459ba0234f"  # workspaces
                },
                timeout=10.0
            )
            
            if resp.status_code != 200:
                return None
            
            # Parse JavaScript response
            text = resp.text
            # Look for workspace ID pattern: id:"wrk_..."
            match = re.search(r'id:"(wrk_[a-zA-Z0-9]+)"', text)
            if match:
                return match.group(1)
            
            return None
        except Exception:
            return None

    async def _get_subscription_data(
        self, 
        client: httpx.AsyncClient, 
        headers: Dict[str, str],
        workspace_id: str
    ) -> Optional[str]:
        """Get subscription/usage data from opencode.ai."""
        try:
            resp = await client.post(
                "https://opencode.ai/_server",
                headers=headers,
                json={
                    "functionId": "7abeebee372f304e050aaaf92be863f4a86490e382f8c79db68fd94040d691b4",  # subscription.get
                    "workspaceId": workspace_id
                },
                timeout=10.0
            )
            
            if resp.status_code != 200:
                return None
            
            return resp.text
        except Exception:
            return None

    def _parse_usage_data(self, text: str) -> List[Dict[str, Any]]:
        """
        Parse JavaScript response to extract usage data.
        
        Expected format:
        rollingUsage:{usagePercent:45.5,resetInSec:7200,limit:12.0}
        weeklyUsage:{usagePercent:23.0,resetInSec:345600,limit:30.0}
        """
        cards = []
        
        # Parse rolling usage (5-hour window)
        rolling_match = re.search(
            r'rollingUsage:\{usagePercent:([\d.]+),resetInSec:(\d+)(?:,limit:([\d.]+))?\}',
            text
        )
        if rolling_match:
            pct = float(rolling_match.group(1))
            reset_sec = int(rolling_match.group(2))
            limit = float(rolling_match.group(3)) if rolling_match.group(3) else 12.0
            
            used = (pct / 100) * limit
            remaining = max(0, limit - used)
            
            # Calculate reset time
            from datetime import datetime, timezone, timedelta
            reset_at = datetime.now(timezone.utc) + timedelta(seconds=reset_sec)
            
            cards.append({
                "service": "OpenCode (5h)",
                "icon": "⚡",
                "remaining": f"${remaining:.2f}",
                "unit": f"${limit:.0f} limit",
                "reset": human_delta(reset_at),
                "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                "pace": "Stable" if pct < 50 else "High" if pct < 80 else "Fatigue",
                "detail": f"${used:.2f} used ({pct:.1f}%) · Web API",
                "used_value": used,
                "limit_value": limit,
                "is_unlimited": False,
                "unit_type": "currency",
                "currency": "USD",
                "reset_at": reset_at.isoformat() if reset_at else None,
                "data_source": "web_api",
            })
        
        # Parse weekly usage
        weekly_match = re.search(
            r'weeklyUsage:\{usagePercent:([\d.]+),resetInSec:(\d+)(?:,limit:([\d.]+))?\}',
            text
        )
        if weekly_match:
            pct = float(weekly_match.group(1))
            reset_sec = int(weekly_match.group(2))
            limit = float(weekly_match.group(3)) if weekly_match.group(3) else 30.0
            
            used = (pct / 100) * limit
            remaining = max(0, limit - used)
            
            from datetime import datetime, timezone, timedelta
            reset_at = datetime.now(timezone.utc) + timedelta(seconds=reset_sec)
            
            cards.append({
                "service": "OpenCode (Weekly)",
                "icon": "⚡",
                "remaining": f"${remaining:.2f}",
                "unit": f"${limit:.0f} limit",
                "reset": human_delta(reset_at),
                "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                "pace": "Stable" if pct < 50 else "High" if pct < 80 else "Fatigue",
                "detail": f"${used:.2f} used ({pct:.1f}%) · Web API",
                "used_value": used,
                "limit_value": limit,
                "is_unlimited": False,
                "unit_type": "currency",
                "currency": "USD",
                "reset_at": reset_at.isoformat() if reset_at else None,
                "data_source": "web_api",
            })
        
        return cards

    async def _get_opencode_tui(self) -> List[Dict[str, Any]]:
        """
        Fetch OpenCode TUI local database statistics with multi-window limits.
        
        This is a fallback when web API and sidecar are unavailable.
        
        Returns:
            List[Dict[str, Any]]: Cards for each time window (5h, week, month)
        """
        db = settings.OPENCODE_DB_PATH
        if not os.path.exists(db):
            return []
        
        try:
            import aiosqlite
            from datetime import datetime, timezone, timedelta
            
            now = datetime.now(timezone.utc)
            
            # Calculate cutoff times for each window
            cutoffs = {
                "5h": int((now - timedelta(hours=5)).timestamp() * 1000),
                "week": int((now - timedelta(days=7)).timestamp() * 1000),
                "month": int((now - timedelta(days=30)).timestamp() * 1000),
            }
            
            # Documented limits for OpenCode Go
            limits = {
                "5h": 12.0,
                "week": 30.0,
                "month": 60.0,
            }
            
            async with aiosqlite.connect(db) as conn:
                cards = []
                
                for window, cutoff_ms in cutoffs.items():
                    cursor = await conn.execute("""
                        SELECT 
                            SUM(json_extract(data, '$.cost')),
                            COUNT(*)
                        FROM message
                        WHERE time_created > ?
                          AND json_valid(data)
                          AND json_extract(data, '$.role') = 'assistant'
                    """, (cutoff_ms,))
                    row = await cursor.fetchone()
                    
                    used = float(row[0] or 0.0)
                    count = int(row[1] or 0)
                    limit = limits[window]
                    remaining = max(0, limit - used)
                    pct = (used / limit * 100) if limit > 0 else 0
                    
                    # Format window label
                    window_labels = {
                        "5h": "5 Hours",
                        "week": "7 Days", 
                        "month": "30 Days"
                    }
                    
                    cards.append({
                        "service": f"OpenCode ({window_labels[window]})",
                        "icon": "⚡",
                        "remaining": f"${remaining:.2f}",
                        "unit": f"${limit:.0f} limit",
                        "reset": f"Rolling {window}",
                        "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
                        "pace": "Stable" if pct < 50 else "High" if pct < 80 else "Fatigue",
                        "detail": f"${used:.2f} used · {count} msgs · Local DB",
                        "used_value": used,
                        "limit_value": limit,
                        "is_unlimited": False,
                        "unit_type": "currency",
                        "currency": "USD",
                        "reset_at": None,  # Rolling window has no fixed reset time
                        "data_source": "local",
                    })
                
                return cards
                
        except Exception as e:
            return [error_card("OpenCode TUI", "⚡", f"DB Error: {str(e)[:15]}")]
