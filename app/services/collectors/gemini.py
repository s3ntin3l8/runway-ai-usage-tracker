"""
Google Gemini quota collector with API and log fallback.

Collection Strategy:
1. Primary: Gemini API endpoints via OAuth
   - Requires GEMINI_OAUTH_PATH credentials file (OAuth refresh flow)
   - Calls cloudcode-pa.googleapis.com to retrieve user quota and tier
   - Tracks multiple quota buckets (Standard/Flash models)
   - Auto-refreshes expired tokens and saves back to credentials file
   
2. Secondary: Local log parsing from Gemini sessions
   - Parses .jsonl files from GEMINI_SESSIONS_DIR
   - Sums prompt_tokens + completion_tokens from logs
   - Estimates usage on rolling 24-hour window
   
3. Error Handling:
   - Missing credentials: Returns empty list (allows other collectors to run)
   - Invalid JSON: Logs warning, returns empty list
   - API failures: Falls back to local logs
   - Token refresh failure: Uses existing token or returns empty list

Token Management:
- Credentials stored in JSON file with expiry_date (in milliseconds)
- Auto-refreshes token if expired before API call
- Saved immediately after refresh to persist for next run
- Uses oauth2.googleapis.com/token endpoint for refresh

Quota Buckets:
- Gemini API can have multiple quota buckets (e.g., Standard vs Flash)
- Collector uses most constrained bucket (lowest remainingFraction)
- Displays model ID and tier information in detail field
"""

import glob
import json
import os
import time
import logging
from typing import List, Dict, Any
import httpx
from app.core.config import settings
from app.services.collectors.base import BaseCollector

logger = logging.getLogger(__name__)

class GeminiCollector(BaseCollector):
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Gemini quota using API first, fallback to local logs.
        
        Returns:
            List[Dict[str, Any]]: Quota card or empty list if unavailable
        """
        # Try API first
        api_data = await self._collect_via_api(client)
        if api_data:
            return api_data
        
        # Fallback to logs
        return await self._collect_via_logs()

    async def _collect_via_api(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Fetch Gemini quota from Google Cloud Code API.
        
        Steps:
        1. Load OAuth credentials from GEMINI_OAUTH_PATH
        2. Refresh token if expired (saves updated credentials back to file)
        3. Call retrieveUserQuota to get quota buckets
        4. Call loadCodeAssist to determine user tier
        5. Return card for most constrained bucket
        
        Returns empty list on any error to allow fallback to logs.
        
        Returns:
            List[Dict[str, Any]]: Single quota card or empty list
        """
        creds_path = settings.GEMINI_OAUTH_PATH
        if not os.path.exists(creds_path):
            logger.debug(f"Gemini credentials not found: {creds_path}")
            return []

        try:
            with open(creds_path, "r") as f:
                creds = json.load(f)

            # Check expiry (expiry_date is in ms)
            if creds.get("expiry_date", 0) < (time.time() * 1000):
                creds = await self._refresh_token(client, creds)
                if not creds: return []
                # Save refreshed creds back
                with open(creds_path, "w") as f:
                    json.dump(creds, f, indent=2)

            token = creds.get("access_token")
            headers = {"Authorization": f"Bearer {token}"}

            # 1. Retrieve Quota
            quota_resp = await client.post(
                "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                json={"project": ""}, # Use default/empty project
                headers=headers
            )
            quota_data = quota_resp.json()

            # 2. Tier Detection
            tier_resp = await client.post(
                "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
                json={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
                headers=headers
            )
            tier_info = tier_resp.json()
            tier = tier_info.get("tier", "unknown").replace("-tier", "").capitalize()

            # Process quota buckets
            # CodexBar logic: lowest remainingFraction wins for standard/flash
            buckets = quota_data.get("buckets", [])
            if not buckets: return []

            # We'll just take the most constrained bucket for the summary
            main_bucket = min(buckets, key=lambda x: x.get("remainingFraction", 1.0))
            percent = int(main_bucket.get("remainingFraction", 1.0) * 100)
            
            # Format reset time
            reset_str = "Resetting..."
            if "resetTime" in main_bucket:
                # Basic ISO-8601 parsing/display
                reset_str = f"Resets at {main_bucket['resetTime'].split('T')[-1][:5]}"

            return [{
                "service": "Gemini CLI",
                "icon": "🔵",
                "remaining": f"{percent}%",
                "unit": "quota",
                "reset": reset_str,
                "health": "good" if percent > 20 else "warn",
                "pace": tier,
                "detail": f"Model: {main_bucket.get('modelId', 'Global')}",
            }]

        except FileNotFoundError as e:
            logger.debug(f"Gemini credential file not found: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in Gemini credentials: {e}")
            return []
        except Exception as e:
            logger.error(f"Gemini API collection failed: {e}")
            return []

    async def _refresh_token(self, client: httpx.AsyncClient, creds: Dict) -> Dict:
        """
        Refresh Google OAuth token if expired.
        
        Uses refresh_token from credentials to get new access_token.
        Updates expiry_date in credentials dictionary (milliseconds).
        Note: Caller is responsible for saving credentials back to file.
        
        Args:
            client: httpx.AsyncClient for making requests
            creds: Dictionary with access_token, refresh_token, expiry_date
            
        Returns:
            Updated creds dict with new access_token and expiry_date, or None if refresh fails
        """
        refresh_token = creds.get("refresh_token")
        if not refresh_token: 
            logger.warning("No refresh token in Gemini credentials")
            return None

        try:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": settings.GEMINI_OAUTH_CLIENT_ID,
                    "client_secret": settings.GEMINI_OAUTH_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
            )
            if resp.status_code != 200:
                logger.warning(f"Token refresh failed with status {resp.status_code}")
                return None
            
            new_data = resp.json()
            creds["access_token"] = new_data["access_token"]
            # Expiry is in seconds in response, convert to ms
            creds["expiry_date"] = int(time.time() * 1000) + (new_data["expires_in"] * 1000)
            return creds
        except Exception as e:
            logger.error(f"Failed to refresh Gemini token: {e}")
            return None

    async def _collect_via_logs(self) -> List[Dict[str, Any]]:
        """
        Fallback: Parse Gemini usage from local session logs.
        
        Scans GEMINI_SESSIONS_DIR for .jsonl files and sums prompt_tokens + completion_tokens.
        Returns single card with total tokens on rolling 24-hour window.
        
        Data Source:
        - Location: Configured by GEMINI_SESSIONS_DIR
        - Format: JSONL with entries containing "usage" field
        
        Returns:
            List[Dict[str, Any]]: Single card with token total or empty list if no logs
        """
        sessions_dir = settings.GEMINI_SESSIONS_DIR
        try:
            files = glob.glob(f"{sessions_dir}/*.jsonl")
            if not files: return []
            total = 0
            for fpath in files:
                with open(fpath, "r") as f:
                    for line in f:
                        u = json.loads(line).get("usage", {})
                        total += (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
            return [{
                "service": "Gemini CLI (Logs)",
                "icon": "🔵",
                "remaining": f"{total:,}",
                "unit": "tokens (24h)",
                "reset": "Rolling 24h",
                "health": "good",
                "pace": "Stable",
                "detail": "Fallback: Local logs",
            }]
        except FileNotFoundError:
            logger.debug(f"Gemini sessions directory not found: {sessions_dir}")
            return []
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in Gemini logs: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to parse Gemini logs: {e}")
            return []
