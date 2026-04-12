"""
Google Gemini quota collector with API and log fallback.
"""

import glob
import json
import os
import time
import asyncio
import logging
import base64
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta
import httpx
from app.core.config import settings
from app.core.utils import error_card, PaceCalculator, safe_write_json, http_request_with_retry
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache
from app.services.credential_provider import credential_provider

from app.services.collectors.oauth_base import OAuthBaseCollector

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


class GeminiCollector(OAuthBaseCollector):
    PROVIDER_ID = "gemini"
    DEFAULT_WINDOW_TYPE = "daily"

    def __init__(self, account_id: Optional[str] = None, account_label: Optional[str] = None):
        """Initialize caching for API results."""
        # Search for credentials via centralized provider
        credentials_path = credential_provider.get_gemini_credentials_path()
        
        # Fallback to default if not found
        if not credentials_path:
            credentials_path = settings.GEMINI_OAUTH_PATH

        super().__init__(
            provider_name="Gemini",
            credentials_path=credentials_path,
            account_id=account_id,
            account_label=account_label,
        )

        self._cached_results = None
        self._last_fetch = None
        self._cache_ttl = 300  # 5 minutes cache for lighter rate limits

    async def _get_current_token(self) -> Optional[str]:
        """Get the current access token."""
        # Check sidecar cache first
        token = await token_cache.get_token(
            "gemini", "oauth_token", account_id=self.account_id
        )
        if not token and not self.account_id:
            creds = await self._get_credentials()
            if creds:
                token = creds.get("access_token")
        return token

    async def _is_token_expired(self) -> bool:
        """Check if Gemini token is expired."""
        try:
            creds = await self._get_credentials()
            if creds:
                expiry_ms = creds.get("expiry_date")
                if expiry_ms:  # Missing or zero → no expiry info, assume still valid
                    return expiry_ms < (time.time() * 1000)
                return False
        except Exception as e:
            logger.debug(f"Could not check Gemini token expiration: {e}")
        return True

    async def _execute_refresh(self, client: httpx.AsyncClient) -> Optional[Dict]:
        """Execute the HTTP request to refresh the token for Gemini."""
        creds = await self._get_credentials()
        if not creds:
            return None

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("No refresh token in Gemini credentials")
            return None

        # Auto-discover client_id from explicit field or id_token if not in settings
        client_id = settings.GEMINI_OAUTH_CLIENT_ID
        if not client_id:
            client_id = creds.get("client_id") or creds.get("clientId")
            
        if not client_id and "id_token" in creds:
            try:
                # JWT payload is the second part, base64 encoded
                parts = creds["id_token"].split(".")
                if len(parts) >= 2:
                    # Add padding if needed
                    payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
                    client_id = payload.get("azp") or payload.get("aud")
                    if client_id:
                        logger.info(f"Auto-discovered Gemini Client ID: {client_id[:10]}...")
            except Exception as e:
                logger.debug(f"Failed to extract Client ID from Gemini id_token: {e}")

        if not client_id:
            logger.warning("Gemini Client ID missing (set GEMINI_OAUTH_CLIENT_ID)")
            return None

        try:
            # Note: client_secret is omitted because public clients (like the CLI) 
            # do not use it for the device/refresh flow.
            resp = await http_request_with_retry(
                client,
                "POST",
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=10,
            )

            if resp.status_code == 200:
                new_data = resp.json()
                creds["access_token"] = new_data["access_token"]
                # Expiry is in seconds in response, convert to ms
                creds["expiry_date"] = int(time.time() * 1000) + (
                    new_data["expires_in"] * 1000
                )

                # Update sidecar cache
                await self._store_sidecar_token("gemini", new_data["access_token"])

                return creds
            else:
                logger.warning(
                    f"Gemini token refresh failed with status {resp.status_code}: {resp.text[:100]}"
                )
                return None
        except Exception as e:
            logger.error(f"Failed to refresh Gemini token: {e}")
            return None

    def _fallback_strategies(self) -> List[Any]:
        """Return the fallback strategies for Gemini (Logs)."""
        return [
            self._collect_via_logs,
        ]

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """API strategy with caching."""
        return await self._collect_via_api_with_cache(client)

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return final error card context when both API and logs fail."""
        # Check if we have credentials to determine the most helpful error
        creds = await self._get_credentials()
        if not creds:
             return [
                error_card(
                    "Gemini",
                    "🔵",
                    "No credentials found",
                    error_type="missing_config",
                )
            ]
        
        return [
            error_card(
                "Gemini", "🔵", "All collection strategies failed", error_type="api_error"
            )
        ]


    async def _collect_via_api_with_cache(
        self, client: httpx.AsyncClient
    ) -> List[Dict[str, Any]]:
        """
        Fetch Gemini quota with caching (cache both success and errors).
        """
        now = datetime.now(timezone.utc)

        # Check cache
        if self._cached_results is not None and self._last_fetch:
            if (now - self._last_fetch).total_seconds() < self._cache_ttl:
                return self._cached_results

        # Fetch fresh data
        results = await self._collect_via_api(client)

        # Cache results: 5m for success, 60s for errors/empty
        if results and not self._is_error_result(results):
            self._cached_results = results
            self._last_fetch = now
        else:
            self._cached_results = results
            self._last_fetch = now - timedelta(seconds=(self._cache_ttl - 60))

        return results

    async def _collect_via_api(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Fetch Gemini quota from Google Cloud Code API."""
        now = datetime.now(timezone.utc)
        backoff_until = getattr(self, "_last_429_backoff_until", None)
        if backoff_until and now < backoff_until:
            wait_rem = (backoff_until - now).total_seconds()
            return [error_card("Gemini", "🔵", f"Rate Limited (429) - Backoff for {wait_rem:.0f}s", error_type="rate_limited")]

        token = await self._get_valid_token(client)
        if not token:
            return []

        try:
            headers = {"Authorization": f"Bearer {token}"}

            # 1. Load Code Assist - get project and tier info
            tier_resp = await http_request_with_retry(
                client,
                "POST",
                "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
                json={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
                headers=headers,
                timeout=10,
            )
            
            if tier_resp.status_code == 429:
                retry_after = tier_resp.headers.get("Retry-After")
                wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
                self._last_429_backoff_until = now + timedelta(seconds=wait_sec)
                return [error_card("Gemini", "🔵", f"Rate Limited (429) - Try in {wait_sec/60:.0f}m", error_type="rate_limited")]

            tier_info = tier_resp.json()
            project_id = tier_info.get("cloudaicompanionProject", "")

            # Extract tier
            paid_tier = tier_info.get("paidTier", {})
            current_tier = tier_info.get("currentTier", {})
            tier_id_raw = paid_tier.get("id", current_tier.get("id", "unknown"))

            tier_mapping = {
                "g1-pro-tier": "pro",
                "g1-ultra-tier": "ultra",
                "standard-tier": "free",
            }
            tier = tier_mapping.get(tier_id_raw, tier_id_raw if tier_id_raw != "unknown" else None)

            # 2. Retrieve Quota
            quota_resp = await http_request_with_retry(
                client,
                "POST",
                "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                json={"project": project_id},
                headers=headers,
                timeout=10,
            )

            if quota_resp.status_code == 429:
                retry_after = quota_resp.headers.get("Retry-After")
                wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
                self._last_429_backoff_until = now + timedelta(seconds=wait_sec)
                return [error_card("Gemini", "🔵", f"Rate Limited (429) - Try in {wait_sec/60:.0f}m", error_type="rate_limited")]

            self._last_429_backoff_until = None
            quota_data = quota_resp.json()
            buckets = quota_data.get("buckets", [])
            
            if not buckets:
                return [error_card("Gemini", "🔵", "No quota buckets returned", error_type="api_error")]

            results = []
            seen_classes = set()

            for bucket in buckets:
                model_id = bucket.get("modelId", "Unknown")
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

                if model_class in seen_classes: continue
                seen_classes.add(model_class)

                remaining_fraction = bucket.get("remainingFraction", 1.0)
                percent_remaining = int(remaining_fraction * 100)
                percent_used = 100 - percent_remaining

                quota_limit = bucket.get("quotaLimit")
                quota_remaining = bucket.get("quotaRemaining")
                token_type = bucket.get("tokenType", "units").lower()

                reset_at = None
                reset_dt = None
                if "resetTime" in bucket:
                    reset_time = bucket["resetTime"]
                    try:
                        reset_dt = datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
                        reset_at = reset_dt.isoformat()
                    except (ValueError, TypeError):
                        pass

                health = "good" if percent_used < 50 else "warning" if percent_used < 80 else "critical"
                pace = PaceCalculator.estimate_longevity(percent_used, reset_dt)

                # Try to discover email from id_token for identity promotion
                email = None
                creds = await self._get_credentials()
                if creds and "id_token" in creds:
                    try:
                        parts = creds["id_token"].split(".")
                        if len(parts) >= 2:
                            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
                            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
                            email = payload.get("email")
                    except Exception:
                        pass

                identity_suffix = f" · {email}" if email else ""

                if quota_limit is not None and quota_remaining is not None:
                    detail_text = f"{percent_remaining}% remaining | {quota_remaining:,} / {quota_limit:,} {token_type} left{identity_suffix}"
                    used_val = float(quota_limit - quota_remaining)
                    limit_val = float(quota_limit)
                else:
                    detail_text = f"{percent_remaining}% remaining | Model: {model_id}{identity_suffix}"
                    used_val = float(percent_used)
                    limit_val = 100.0

                results.append({
                    "service_name": display_name,
                    "icon": "🔵",
                    "remaining": f"{percent_used}%",
                    "unit": "used",
                    "reset": reset_at,
                    "health": health,
                    "pace": pace,
                    "detail": detail_text,
                    "used_value": used_val,
                    "limit_value": limit_val,
                    "is_unlimited": False,
                    "unit_type": token_type if quota_limit is not None else "percent",
                    "reset_at": reset_at,
                    "data_source": "oauth",
                    "tier": tier,
                    "usage_url": "https://one.google.com/settings",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

            results.sort(key=lambda x: int(x["remaining"].rstrip("%")), reverse=True)
            return results

        except Exception as e:
            logger.error(f"Gemini API collection failed: {e}")
            return [error_card("Gemini", "🔵", f"API Error: {str(e)[:30]}", error_type="api_error")]

    async def _collect_via_logs(self, client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, Any]]:
        """Fallback: Parse Gemini usage from local session logs."""
        potential_dirs = [
            settings.GEMINI_SESSIONS_DIR,
            os.path.expanduser("~/.gemini/tmp/sessions"),
            os.path.expanduser("~/.gemini/sessions"),
            os.path.expanduser("~/.gemini/tmp"),
        ]

        files = []
        try:
            existing_dirs = [d for d in potential_dirs if os.path.isdir(d)]
            if existing_dirs:
                import glob
                results = await asyncio.gather(*[asyncio.to_thread(glob.glob, f"{d}/*.jsonl") for d in existing_dirs])
                for found in results: files.extend(found)

            if not files: return []

            def process_logs(fpaths):
                total = 0
                for fpath in fpaths:
                    with open(fpath, "r") as f:
                        for line in f:
                            u = json.loads(line).get("usage", {})
                            total += u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
                return total

            total = await asyncio.to_thread(process_logs, files)
            return [{
                "service_name": "Gemini CLI (Logs)",
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
        except Exception:
            return []
