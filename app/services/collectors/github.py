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
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import httpx
from app.core.config import settings
from app.services.credential_provider import credential_provider
from app.core.utils import human_delta, error_card, PaceCalculator, http_request_with_retry
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class GitHubCollector(BaseCollector):
    PROVIDER_ID = "github"
    DEFAULT_WINDOW_TYPE = "monthly"

    def __init__(self, account_id: Optional[str] = None, account_label: Optional[str] = None):
        """Initialize orchestrator."""
        super().__init__(account_id=account_id, account_label=account_label)
        self._cached_results = None
        self._last_fetch = None
        self._cache_ttl = 300  # 5 minutes cache for lighter rate limits


    def _fallback_strategies(self) -> List[Any]:
        """Return the fallback strategies for GitHub (None)."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Fetch GitHub Copilot quota with caching."""
        return await self._strategy_api(client)

    async def _error_handler(self) -> List[Dict[str, Any]]:
        """Return final error card context when both API and fallback fail."""
        token = await self._get_token()

        if not token:
            return [
                error_card(
                    "GitHub Copilot",
                    "🐙",
                    "Login required to fetch limits",
                    error_type="auth_failed",
                )
            ]

        return [
            error_card(
                "GitHub Copilot", "🐙", "All collection strategies failed", error_type="api_error"
            )
        ]

    async def _strategy_api(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """Fetch GitHub Copilot quota with caching."""
        token = await self._get_token()
        if not token:
            return []

        # Check cache
        now = datetime.now(timezone.utc)
        if self._cached_results is not None and self._last_fetch:
            if (now - self._last_fetch).total_seconds() < self._cache_ttl:
                return self._cached_results

        # Proactive backoff check
        backoff_until = getattr(self, "_last_429_backoff_until", None)
        if backoff_until and now < backoff_until:
            wait_rem = (backoff_until - now).total_seconds()
            logger.debug(f"Proactively skipping GitHub API call due to recent 429 (backoff for {wait_rem:.0f}s)")
            return [error_card("GitHub Copilot", "🐙", f"Rate Limited (429) - Backoff for {wait_rem:.0f}s", error_type="rate_limited")]

        try:
            headers = {
                "Authorization": f"token {token}",
                "X-GitHub-Api-Version": "2025-04-01",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.96.2",
                "Editor-Plugin-Version": "copilot-chat/0.26.7",
                "User-Agent": "GitHubCopilotChat/0.26.7",
            }

            # 1. Fetch User/Quota Info first
            user_resp = await http_request_with_retry(
                client,
                "GET",
                "https://api.github.com/copilot_internal/user",
                headers=headers,
                timeout=10.0,
            )

            token_resp = None
            user_data = {}

            if user_resp.status_code == 200:
                user_data = user_resp.json()
                has_snapshots = bool(user_data.get("quota_snapshots"))
                has_limited_info = (
                    "limited_user_quotas" in user_data
                    and "limited_user_reset_date" in user_data
                )
                if not (has_snapshots or has_limited_info):
                    token_resp = await http_request_with_retry(
                        client,
                        "GET",
                        "https://api.github.com/copilot_internal/v2/token",
                        headers=headers,
                        timeout=10.0,
                    )
            elif user_resp.status_code == 429:
                # Set proactive backoff based on Retry-After or default 5m
                retry_after = user_resp.headers.get("Retry-After")
                wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
                self._last_429_backoff_until = now + timedelta(seconds=wait_sec)
                logger.warning(f"GitHub API returned 429. Proactive backoff set for {wait_sec}s")
                return [error_card("GitHub Copilot", "🐙", f"Rate Limited (429) - Try in {wait_sec/60:.0f}m", error_type="rate_limited")]
            else:
                token_resp = await http_request_with_retry(
                    client,
                    "GET",
                    "https://api.github.com/copilot_internal/v2/token",
                    headers=headers,
                    timeout=10.0,
                )

            # Check token_resp for 429 too
            if token_resp and token_resp.status_code == 429:
                retry_after = token_resp.headers.get("Retry-After")
                wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
                self._last_429_backoff_until = now + timedelta(seconds=wait_sec)
                return [error_card("GitHub Copilot", "🐙", f"Rate Limited (429) - Try in {wait_sec/60:.0f}m", error_type="rate_limited")]

            # Success: Clear any backoff
            self._last_429_backoff_until = None

            # Try to discover identity from local gh config for promotion
            identity = None
            gh_config_path = os.path.expanduser("~/.config/gh/hosts.yml")
            if os.path.exists(gh_config_path):
                try:
                    import yaml
                    with open(gh_config_path, "r") as f:
                        config = yaml.safe_load(f)
                        host_config = config.get("github.com", {})
                        identity = host_config.get("user") or list(host_config.get("users", {}).keys())[0]
                except Exception:
                    pass
            
            self._identity = identity
            cards = self._parse_api_responses(user_resp, token_resp, user_data)
            # Cache results (including empty/error cards) to avoid hammering API
            self._cached_results = cards
            self._last_fetch = now
            return cards
        except Exception as e:
            logger.debug(f"GitHub Copilot API strategy failed: {e}")
            self._cached_results = []
            self._last_fetch = now

        return []


    async def _get_token(self) -> Optional[str]:
        """Internal helper to get token from multiple sources."""
        token = credential_provider.get_github_token()
        if token:
            return token

        if self.account_id:
            token = await token_cache.get_token("github", "api_key", account_id=self.account_id)
        return token

    def _parse_api_responses(self, user_resp, token_resp, user_data) -> List[Dict[str, Any]]:
        """Consolidate the parsing logic from collect()."""
        cards = []
        
        # Process Token Response
        if token_resp and token_resp.status_code == 200:
            token_data = token_resp.json()
            if "limited_user_quotas" in token_data:
                cards.extend(self._parse_limited_quotas(token_data, "[Free/Limited Tier]"))

        # Process User Response
        if user_resp.status_code == 200:
            if "limited_user_quotas" in user_data:
                # Avoid duplicates if v2/token also returned them
                if not any(c["service_name"].startswith("Copilot") for c in cards):
                    cards.extend(self._parse_limited_quotas(user_data, "• Free Tier"))

            # Process snapshots
            snapshots = user_data.get("quota_snapshots", [])
            plan = user_data.get("copilot_plan", "Individual")
            cards.extend(self._parse_quota_snapshots(snapshots, plan))

        return cards

    def _parse_limited_quotas(self, data: Dict[str, Any], detail_context: str) -> List[Dict[str, Any]]:
        """Parse limited_user_quotas structure."""
        results = []
        quotas = data["limited_user_quotas"]
        monthly = data.get("monthly_quotas", {})
        reset_date = data.get("limited_user_reset_date")
        reset_at = None
        if reset_date:
            try:
                reset_at = datetime.fromisoformat(reset_date.replace("Z", "+00:00"))
            except (ValueError, TypeError) as e:
                logger.debug(f"Could not parse GitHub reset date '{reset_date}': {e}")

        for key in ["completions", "chat"]:
            if key in quotas:
                val = quotas[key]
                monthly_val = monthly.get(key, 100)
                used_val = monthly_val - val if isinstance(monthly_val, int) else 0
                pct_used = (used_val / monthly_val * 100) if isinstance(monthly_val, (int, float)) and monthly_val > 0 else 0
                pace = PaceCalculator.estimate_longevity(pct_used, reset_at)
                identity_suffix = f" · {self._identity}" if getattr(self, "_identity", None) else ""
                results.append({
                    "service_name": f"Copilot ({key.title()})",
                    "icon": "🐙",
                    "remaining": f"{val:,}",
                    "unit": (f"/ {monthly_val:,}" if isinstance(monthly_val, int) else "remaining"),
                    "reset": reset_at.isoformat() if reset_at else None,
                    "health": "good" if val > 10 else "warning",
                    "pace": pace,
                    "detail": f"{val}/{monthly_val if isinstance(monthly_val, int) else '??'} requests left {detail_context}{identity_suffix}",
                    "used_value": float(used_val),
                    "limit_value": float(monthly_val) if isinstance(monthly_val, (int, float)) else 100.0,
                    "is_unlimited": False,
                    "unit_type": "requests",
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": "api",
                    "usage_url": "https://github.com/settings/copilot/features",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
        return results

    def _parse_quota_snapshots(self, snapshots: List[Dict], plan: str) -> List[Dict[str, Any]]:
        """Parse quota_snapshots structure."""
        results = []
        metric_map = {
            "premium_interactions": "Premium Interactions",
            "chat": "Chat Usage",
            "completions": "Autocomplete",
        }
        tier_map = {"individual": "pro", "business": "team", "enterprise": "enterprise"}
        tier_name = tier_map.get(plan.lower(), plan.lower()) if plan else None

        for snap in snapshots:
            metric_raw = snap.get("metric", "unknown")
            metric = metric_map.get(metric_raw, metric_raw.replace("_", " ").title())
            rem = snap.get("remaining")
            ent = snap.get("entitlement")

            if rem is not None and ent is not None:
                used_val = ent - rem
                pct_used = (used_val / ent * 100) if ent > 0 else 0
                pace = PaceCalculator.estimate_longevity(pct_used, None)
                results.append({
                    "service_name": f"Copilot ({metric})",
                    "icon": "🐙",
                    "remaining": f"{rem:,}",
                    "unit": f"/ {ent:,}",
                    "reset": "Rolling",
                    "health": "good" if (ent > 0 and (rem / ent) > 0.3) else "warning",
                    "pace": pace,
                    "detail": f"{pct_used:.1f}% used • {plan} [Pro Tier]",
                    "used_value": float(used_val),
                    "limit_value": float(ent),
                    "is_unlimited": False,
                    "tier": tier_name,
                    "unit_type": "requests",
                    "reset_at": None,
                    "data_source": "api",
                    "usage_url": "https://github.com/settings/copilot/features",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
        return results
