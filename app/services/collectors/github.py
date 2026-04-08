"""
GitHub Copilot quota collector with tier-aware fallback.

Collection Strategy:
1. Primary: GitHub Copilot API endpoints (authenticated with GITHUB_TOKEN)
   - Requires GITHUB_TOKEN environment variable
   - Calls copilot_internal/v2/token for free/limited user quotas
   - Calls copilot_internal/user for pro/enterprise quota snapshots
   - Returns cards for: Completions, Chat, Premium Interactions, etc.
   
2. Fallback: Standard GitHub API rate limits
   - If Copilot-specific endpoints unavailable, falls back to /rate_limit
   - Shows core API request quota as proxy for usage
   
3. Error Handling:
   - Missing token: Returns empty list
   - API errors: Returns error card with first 15 chars of error message
   
Data Details:
- Free/Limited Tier: limited_user_quotas (e.g., "completions", "chat")
  Includes reset_date for when quotas reset
- Pro/Enterprise: quota_snapshots with individual metrics
  Each snapshot has remaining and entitlement counts
  Computes percentage used and health status

Headers:
- Mimics VS Code Copilot extension to improve API reliability
- Includes editor version and plugin version headers
"""

import os
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any
import httpx
from app.core.config import settings
from app.core.utils import human_delta, error_card
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

class GitHubCollector(BaseCollector):
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect GitHub Copilot quota for free, pro, and enterprise tiers.
        
        Queries:
        1. copilot_internal/v2/token - Limited user quotas (free tier)
        2. copilot_internal/user - Pro tier quota snapshots
        3. /rate_limit - Fallback to GitHub API rate limits if above unavailable
        
        Token priority:
        1. GITHUB_TOKEN env var
        2. Token cache from sidecar
        
        Returns:
            List[Dict[str, Any]]: Cards for each quota type or error card
        """
        # Check for token (env var or sidecar cache)
        token = settings.GITHUB_TOKEN
        if not token:
            token = token_cache.get_token("github", "api_key")
            if token:
                logger.info("Using GitHub token from sidecar cache")
        
        if not token:
            return []
        try:
            # Use Copilot internal endpoints for detailed metrics
            # Mimicking VS Code headers as suggested by CodexBar for better reliability
            headers = {
                "Authorization": f"token {token}",
                "X-GitHub-Api-Version": "2025-04-01",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.96.2",
                "Editor-Plugin-Version": "copilot-chat/0.26.7",
                "User-Agent": "GitHubCopilotChat/0.26.7"
            }
            
            # 1. Fetch User/Quota Info first (Main source for Pro and Enterprise)
            user_resp = await client.get("https://api.github.com/copilot_internal/user", headers=headers)
            
            # 2. Determine if we need to call v2/token (primarily for Free/Limited tier reset dates)
            token_resp = None
            user_data = {}
            
            if user_resp.status_code == 200:
                user_data = user_resp.json()
                
                # If we have snapshots, it's Pro/Enterprise, v2/token is likely 403 Forbidden
                # If we have limited_user_quotas AND limited_user_reset_date, we already have everything
                has_snapshots = bool(user_data.get("quota_snapshots"))
                has_limited_info = "limited_user_quotas" in user_data and "limited_user_reset_date" in user_data
                
                if not (has_snapshots or has_limited_info):
                    token_resp = await client.get("https://api.github.com/copilot_internal/v2/token", headers=headers)
            else:
                # If /user failed (e.g., 404 or 403), try /v2/token as a fallback
                token_resp = await client.get("https://api.github.com/copilot_internal/v2/token", headers=headers)
            
            cards = []
            
            # Process Token Response (Free/Limited Tier specific)
            if token_resp and token_resp.status_code == 200:
                token_data = token_resp.json()
                if "limited_user_quotas" in token_data:
                    quotas = token_data["limited_user_quotas"]
                    reset_date = token_data.get("limited_user_reset_date")
                    reset_at = None
                    if reset_date:
                        try: reset_at = datetime.fromisoformat(reset_date.replace("Z", "+00:00"))
                        except: pass
                    
                    for key in ["completions", "chat"]:
                        if key in quotas:
                            val = quotas[key]
                            # Free tier typically has limits around 50-100 requests
                            estimated_limit = 100
                            used = max(0, estimated_limit - val)
                            cards.append({
                                "service": f"Copilot ({key.title()})",
                                "icon": "🐙",
                                "remaining": f"{val:,}",
                                "unit": "remaining",
                                "reset": human_delta(reset_at),
                                "health": "good" if val > 10 else "warning",
                                "pace": "Manual",
                                "detail": f"{val} requests left [Free/Limited Tier]",
                                "used_value": float(used),
                                "limit_value": float(estimated_limit),
                                "is_unlimited": False,
                                "unit_type": "requests",
                                "reset_at": reset_at.isoformat() if reset_at else None,
                                "data_source": "api",
                            })

            # Process User Response (Pro/Enterprise and Free fallback)
            if user_resp.status_code == 200:
                # user_data already parsed above
                
                # Check for free/limited tier quotas in user response
                if "limited_user_quotas" in user_data:
                    quotas = user_data["limited_user_quotas"]
                    monthly = user_data.get("monthly_quotas", {})
                    reset_date = user_data.get("limited_user_reset_date")
                    reset_at = None
                    if reset_date:
                        try: reset_at = datetime.fromisoformat(reset_date.replace("Z", "+00:00"))
                        except: pass
                    
                    for key in ["completions", "chat"]:
                        if key in quotas:
                            val = quotas[key]
                            monthly_val = monthly.get(key, 100)
                            used_val = monthly_val - val if isinstance(monthly_val, int) else 0
                            cards.append({
                                "service": f"Copilot ({key.title()})",
                                "icon": "🐙",
                                "remaining": f"{val:,}",
                                "unit": f"/ {monthly_val:,}",
                                "reset": human_delta(reset_at),
                                "health": "good" if val > (monthly_val * 0.3 if isinstance(monthly_val, int) else 10) else "warning" if val > (monthly_val * 0.1 if isinstance(monthly_val, int) else 5) else "critical",
                                "pace": "Manual",
                                "detail": f"{val}/{monthly_val} requests left • Free Tier",
                                "used_value": float(used_val),
                                "limit_value": float(monthly_val) if isinstance(monthly_val, (int, float)) else 100.0,
                                "is_unlimited": False,
                                "tier": "Free",
                                "unit_type": "requests",
                                "reset_at": reset_at.isoformat() if reset_at else None,
                                "data_source": "api",
                            })
                
                # Check for Pro/Enterprise tier quota snapshots
                snapshots = user_data.get("quota_snapshots", [])
                plan = user_data.get("copilot_plan", "Individual")
                
                for snap in snapshots:
                    metric_raw = snap.get("metric", "unknown")
                    # Map internal names to user-friendly titles
                    metric_map = {
                        "premium_interactions": "Premium Interactions",
                        "chat": "Chat Usage",
                        "completions": "Autocomplete"
                    }
                    metric = metric_map.get(metric_raw, metric_raw.replace("_", " ").title())

                    rem = snap.get("remaining")
                    ent = snap.get("entitlement")

                    if rem is not None and ent is not None:
                        used_val = ent - rem
                        pct_used = (used_val / ent * 100) if ent > 0 else 0
                        cards.append({
                            "service": f"Copilot ({metric})",
                            "icon": "🐙",
                            "remaining": f"{rem:,}",
                            "unit": f"/ {ent:,}",
                            "reset": "Rolling",
                            "health": "good" if (ent > 0 and (rem/ent) > 0.3) else "warning" if (ent > 0 and (rem/ent) > 0.1) else "critical",
                            "pace": "Sustainable" if pct_used < 70 else "Fatigue",
                            "detail": f"{pct_used:.1f}% used • {plan} [Pro Tier]",
                            "used_value": float(used_val),
                            "limit_value": float(ent),
                            "is_unlimited": False,
                            "tier": plan,
                            "unit_type": "requests",
                            "reset_at": None,  # Rolling quotas have no fixed reset time
                            "data_source": "api",
                        })
            
            # Fallback to standard rate limit if no specific copilot data found
            if not cards:
                resp = await client.get("https://api.github.com/rate_limit", headers={"Authorization": f"Bearer {token}"})
                if resp.status_code == 200:
                    data = resp.json()["resources"]["core"]
                    rem, lim = data["remaining"], data["limit"]
                    used = lim - rem
                    reset_at = datetime.fromtimestamp(data["reset"], tz=timezone.utc)
                    cards.append({
                        "service": "GitHub API",
                        "icon": "🐙",
                        "remaining": f"{rem:,}",
                        "unit": "requests",
                        "reset": human_delta(reset_at),
                        "health": "good" if rem/lim > 0.3 else "warning",
                        "pace": "Stable",
                        "detail": f"{rem}/{lim} [API fallback]",
                        "used_value": float(used),
                        "limit_value": float(lim),
                        "is_unlimited": False,
                        "unit_type": "requests",
                        "reset_at": reset_at.isoformat() if reset_at else None,
                        "data_source": "fallback",
                    })
            
            return cards
        except Exception as e:
            return [error_card("GitHub Copilot", "🐙", f"Fail: {str(e)[:15]}", error_type="api_error")]
