import glob
import json
import os
import time
from typing import List, Dict, Any
import httpx
from app.core.config import settings
from app.services.collectors.base import BaseCollector

class GeminiCollector(BaseCollector):
    OAUTH_CLIENT_ID = "YOUR_CLIENT_ID_HERE"
    OAUTH_CLIENT_SECRET = "YOUR_CLIENT_SECRET_HERE"

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        # Try API first
        api_data = await self._collect_via_api(client)
        if api_data:
            return api_data
        
        # Fallback to logs
        return await self._collect_via_logs()

    async def _collect_via_api(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        creds_path = settings.GEMINI_OAUTH_PATH
        if not os.path.exists(creds_path):
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

        except Exception as e:
            # Silently fail for API and let fallback handle it
            return []

    async def _refresh_token(self, client: httpx.AsyncClient, creds: Dict) -> Dict:
        refresh_token = creds.get("refresh_token")
        if not refresh_token: return None

        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self.OAUTH_CLIENT_ID,
                "client_secret": self.OAUTH_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        )
        if resp.status_code != 200:
            return None
        
        new_data = resp.json()
        creds["access_token"] = new_data["access_token"]
        # Expiry is in seconds in response, convert to ms
        creds["expiry_date"] = int(time.time() * 1000) + (new_data["expires_in"] * 1000)
        return creds

    async def _collect_via_logs(self) -> List[Dict[str, Any]]:
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
        except: return []
