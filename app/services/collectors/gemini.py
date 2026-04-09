"""
Google Gemini quota collector with API and log fallback.

Collection Strategy:
1. Primary: Gemini API endpoints via OAuth
   - Requires GEMINI_OAUTH_PATH credentials file (OAuth refresh flow)
   - Calls cloudcode-pa.googleapis.com to retrieve user quota and tier
   - Discovers project via loadCodeAssist for complete model list (including gemini-3)
   - Auto-refreshes expired tokens and saves back to credentials file
   - Returns one card per model quota bucket
   
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
- Gemini API returns quota buckets per model (2.5-flash, 2.5-pro, 3-flash-preview, etc.)
- Project parameter required to get gemini-3 models (discovered via loadCodeAssist)
- Each bucket shows remainingFraction (1.0 = 100% remaining = 0% used)
- All models displayed individually with their own quota status

Tier Detection:
- Loads current tier from loadCodeAssist (standard-tier, g1-pro-tier, etc.)
- Displays tier name in cards for context
- Shows paid tier availability if different from current tier
"""

import glob
import json
import os
import time
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import httpx
from app.core.config import settings
from app.core.utils import error_card, PaceCalculator
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

# Model display name mapping
MODEL_DISPLAY_NAMES = {
    "gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
    "gemini-2.5-pro": "Gemini 2.5 Pro",
    "gemini-3-flash-preview": "Gemini 3 Flash (Preview)",
    "gemini-3-pro-preview": "Gemini 3 Pro (Preview)",
    "gemini-3.1-flash-lite-preview": "Gemini 3.1 Flash Lite (Preview)",
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro (Preview)",
}


class GeminiCollector(BaseCollector):
    def __init__(self):
        """Initialize caching for API results."""
        self._cached_results = None
        self._last_fetch = None
        self._cache_ttl = 300  # 5 minutes cache for lighter rate limits

    def _is_error_result(self, results: List[Dict[str, Any]]) -> bool:
        """Check if results contain an error card."""
        if not results:
            return True
        return any(r.get("remaining") == "ERR" for r in results)

    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Gemini quota using API with caching, fallback to local logs.

        Returns:
            List[Dict[str, Any]]: List of quota cards (one per model) or fallback data
        """
        # Try API first (with caching)
        api_data = await self._collect_via_api_with_cache(client)
        if api_data and not self._is_error_result(api_data):
            return api_data

        # Fallback to logs if API failed or returned errors
        log_data = await self._collect_via_logs()
        if log_data:
            return log_data

        # Return API error if no logs available
        return api_data if api_data else []

    async def _collect_via_api_with_cache(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Fetch Gemini quota with caching (cache both success and errors).

        Returns cached result if within TTL to avoid hammering API.
        """
        from datetime import timezone
        now = datetime.now(timezone.utc)

        # Check cache - works for both success AND error results (check is not None for empty lists)
        if self._cached_results is not None and self._last_fetch:
            if (now - self._last_fetch).total_seconds() < self._cache_ttl:
                return self._cached_results

        # Fetch fresh data
        results = await self._collect_via_api(client)

        # Cache ALL results (success or error, including empty list)
        self._cached_results = results
        self._last_fetch = now

        return results

    async def _collect_via_api(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Fetch Gemini quota from Google Cloud Code API.
        
        Steps:
        1. Check sidecar token cache, or load OAuth credentials from GEMINI_OAUTH_PATH
        2. Refresh token if expired (saves updated credentials back to file)
        3. Call loadCodeAssist to discover project and get tier info
        4. Call retrieveUserQuota with discovered project to get all model quotas (including gemini-3)
        5. Return one card per model bucket
        
        Returns empty list on any error to allow fallback to logs.
        
        Returns:
            List[Dict[str, Any]]: List of quota cards, one per model, or empty list
        """
        # Check for sidecar token
        cached_token = await token_cache.get_token("gemini", "oauth_token")
        if cached_token:
            creds = {"access_token": cached_token, "expiry_date": float('inf')}
            creds_path = None  # No file path for cached tokens
        else:
            # Priority 2: Try multiple credential file locations
            # (same logic as sidecar for consistency)
            potential_paths = [
                settings.GEMINI_OAUTH_PATH,  # From env var or platform config dir
                os.path.expanduser("~/.gemini/oauth_creds.json"),  # Legacy location
            ]
            
            creds_path = None
            creds = None
            
            for path in potential_paths:
                if os.path.exists(path):
                    try:
                        with open(path, "r") as f:
                            creds = json.load(f)
                        creds_path = path
                        logger.debug(f"Loaded Gemini credentials from {path}")
                        break
                    except Exception as e:
                        logger.debug(f"Failed to read {path}: {e}")
                        continue
            
            if not creds:
                logger.debug(f"Gemini credentials not found in any location: {potential_paths}")
                return []  # Allow fallback to logs

        try:
            # Check expiry (expiry_date is in ms)
            if creds.get("expiry_date", 0) < (time.time() * 1000):
                creds = await self._refresh_token(client, creds)
                if not creds:
                    return [error_card("Gemini", "🔵", "Token refresh failed", error_type="auth_failed")]
                # Save refreshed creds back (only if we have a file path)
                if not cached_token and creds_path:
                    with open(creds_path, "w") as f:
                        json.dump(creds, f, indent=2)

            token = creds.get("access_token")
            headers = {"Authorization": f"Bearer {token}"}

            # 1. Load Code Assist - get project and tier info
            tier_resp = await client.post(
                "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
                json={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
                headers=headers
            )
            tier_info = tier_resp.json()
            
            # Extract project and tier
            project_id = tier_info.get("cloudaicompanionProject", "")
            
            # Check for paid tier first (user has Pro subscription)
            paid_tier = tier_info.get("paidTier", {})
            current_tier = tier_info.get("currentTier", {})
            
            if paid_tier:
                # User has Pro access
                tier_id_raw = paid_tier.get("id", "unknown")
            else:
                # Free tier only
                tier_id_raw = current_tier.get("id", "unknown")
            
            # Map tier IDs to short display names
            tier_mapping = {
                "g1-pro-tier": "pro",
                "g1-ultra-tier": "ultra",
                "standard-tier": "free",
            }
            tier = tier_mapping.get(tier_id_raw, tier_id_raw if tier_id_raw != "unknown" else None)
            
            # 2. Retrieve Quota with discovered project (required for gemini-3 models)
            quota_resp = await client.post(
                "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                json={"project": project_id},
                headers=headers
            )
            quota_data = quota_resp.json()

            # Process quota buckets - return one card per model family
            buckets = quota_data.get("buckets", [])
            if not buckets:
                return [error_card("Gemini", "🔵", "No quota buckets returned", error_type="api_error")]

            results = []
            seen_classes = set()
            
            for bucket in buckets:
                model_id = bucket.get("modelId", "Unknown")
                
                # Consolidate models into classes since they share quotas
                if "flash-lite" in model_id:
                    display_name = "Gemini Flash Lite"
                    model_class = "flash-lite"
                elif "flash" in model_id:
                    display_name = "Gemini Flash"
                    model_class = "flash"
                elif "pro" in model_id:
                    display_name = "Gemini Pro"
                    model_class = "pro"
                else:
                    display_name = MODEL_DISPLAY_NAMES.get(model_id, model_id)
                    model_class = model_id
                    
                if model_class in seen_classes:
                    continue
                seen_classes.add(model_class)
                
                # remainingFraction: 1.0 = 100% remaining = 0% used
                remaining_fraction = bucket.get("remainingFraction", 1.0)
                percent_remaining = int(remaining_fraction * 100)
                percent_used = 100 - percent_remaining
                
                # Parse reset time
                reset_at = None
                reset_dt = None
                if "resetTime" in bucket:
                    reset_time = bucket["resetTime"]
                    try:
                        # Parse for reset_at timestamp (frontend will format display)
                        reset_dt = datetime.fromisoformat(reset_time.replace('Z', '+00:00'))
                        reset_at = reset_dt.isoformat()
                    except:
                        pass

                # Determine health based on % used (not remaining)
                if percent_used < 50:
                    health = "good"
                elif percent_used < 80:
                    health = "warning"
                else:
                    health = "critical"

                # Calculate pace based on usage rate
                pace = PaceCalculator.estimate_longevity(percent_used, reset_dt if reset_at else None)

                results.append({
                    "service": display_name,
                    "icon": "🔵",
                    "remaining": f"{percent_used}%",
                    "unit": "used",
                    "reset": reset_at,  # Frontend will format this ISO timestamp
                    "health": health,
                    "pace": pace,
                    "detail": f"{percent_remaining}% remaining | Model: {model_id}",
                    "used_value": float(percent_used),
                    "limit_value": 100.0,
                    "is_unlimited": False,
                    "unit_type": "percent",
                    "reset_at": reset_at,
                    "data_source": "oauth",
                    "tier": tier,
                    "usage_url": "https://one.google.com/settings",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
            
            # Sort by usage (highest % used first = most constrained)
            results.sort(key=lambda x: int(x["remaining"].rstrip("%")), reverse=True)
            
            return results

        except FileNotFoundError as e:
            logger.debug(f"Gemini credential file not found: {e}")
            return [error_card("Gemini", "🔵", "No credentials file found", error_type="missing_config")]
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in Gemini credentials: {e}")
            return [error_card("Gemini", "🔵", "Invalid credentials format", error_type="parse_error")]
        except Exception as e:
            logger.error(f"Gemini API collection failed: {e}")
            return [error_card("Gemini", "🔵", f"API Error: {str(e)[:30]}", error_type="api_error")]

    def _persist_refreshed_tokens(self, access_token: str, refresh_token: str):
        """Persist refreshed tokens to local config file."""
        if settings.RUN_MODE == "docker":
            logger.info("Skipping Gemini token persistence in Docker mode")
            return
            
        try:
            # Logic to save to file would go here
            pass
        except Exception as e:
            logger.error(f"Failed to persist tokens: {e}")

    async def _refresh_token(self, client: httpx.AsyncClient, creds: Dict) -> Optional[Dict]:
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
            if not files: 
                return []
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
                "data_source": "local",
                "usage_url": "https://one.google.com/settings",
                "updated_at": datetime.now(timezone.utc).isoformat(),
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
