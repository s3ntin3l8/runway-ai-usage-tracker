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

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import (
    HealthCalculator,
    IdentityExtractor,
    PaceCalculator,
    error_card,
    http_request_with_retry,
)
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class GitHubCollector(BaseCollector):
    PROVIDER_ID = "github"
    DEFAULT_WINDOW_TYPE = "monthly"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        """Initialize orchestrator."""
        super().__init__(account_id=account_id, account_label=account_label)
        self._cached_results = None
        self._last_fetch = None
        self._cache_ttl = 300  # 5 minutes cache for lighter rate limits

    async def is_configured(self) -> bool:
        """Check if GitHub token is present."""
        return bool(await self._get_token())

    def _fallback_strategies(self) -> list[Any]:
        """Return the fallback strategies for GitHub (None)."""
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fetch GitHub Copilot quota with caching."""
        return await self._strategy_api(client)

    async def _error_handler(self) -> list[dict[str, Any]]:
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

    async def _strategy_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:  # noqa: PLR0915 — known-debt: GitHub API response handling, refactor tracked separately
        """Fetch GitHub Copilot quota with caching."""
        token = await self._get_token()
        if not token:
            logger.warning(
                "GitHub collector: no token resolved for account_id=%r — skipping collection",
                self.account_id or "default",
            )
            return []

        # Check cache
        now = datetime.now(UTC)
        if self._cached_results is not None and self._last_fetch:
            age = (now - self._last_fetch).total_seconds()
            if age < self._cache_ttl:
                logger.debug("GitHub collector: returning cached results (age=%.0fs)", age)
                return self._cached_results

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
                    "limited_user_quotas" in user_data and "limited_user_reset_date" in user_data
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
                # Pass Retry-After to SmartCollector for centralized backoff
                retry_after = user_resp.headers.get("Retry-After")
                wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
                self._last_retry_after = wait_sec
                logger.warning(f"GitHub API returned 429. Retry-After: {wait_sec}s")
                return [
                    error_card(
                        "GitHub Copilot",
                        "🐙",
                        f"Rate Limited (429) - Try in {wait_sec / 60:.0f}m",
                        error_type="rate_limited",
                    )
                ]
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
                self._last_retry_after = wait_sec
                return [
                    error_card(
                        "GitHub Copilot",
                        "🐙",
                        f"Rate Limited (429) - Try in {wait_sec / 60:.0f}m",
                        error_type="rate_limited",
                    )
                ]

            # Try to discover identity — call the standard /user endpoint (not Copilot internal)
            identity = getattr(self, "_identity", None)

            # Use sidecar-provided label if it looks like an email
            if not identity and self.account_label and "@" in self.account_label:
                identity = self.account_label

            if not identity:
                try:
                    # Use clean headers for standard endpoints (avoid futuristic Copilot API version)
                    std_headers = {
                        "Authorization": f"token {token}",
                        "Accept": "application/json",
                        "User-Agent": "Runway-AI-Usage-Tracker",
                    }

                    user_std_resp = await http_request_with_retry(
                        client,
                        "GET",
                        "https://api.github.com/user",
                        headers=std_headers,
                        timeout=10.0,
                    )
                    if user_std_resp.status_code == 200:
                        std_data = user_std_resp.json()
                        # Try email from main profile
                        identity = std_data.get("email")

                        # Try /user/emails for private email addresses
                        if not identity:
                            emails_resp = await http_request_with_retry(
                                client,
                                "GET",
                                "https://api.github.com/user/emails",
                                headers=std_headers,
                                timeout=10.0,
                            )
                            if emails_resp.status_code == 200:
                                emails = emails_resp.json()
                                identity = IdentityExtractor.extract_best_email(emails)

                        # Fallback to local git config user.email if still missing
                        if not identity:
                            try:
                                proc = await asyncio.create_subprocess_exec(
                                    "git",
                                    "config",
                                    "--global",
                                    "user.email",
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                stdout, _ = await proc.communicate()
                                if proc.returncode == 0:
                                    git_email = stdout.decode().strip()
                                    if git_email:
                                        identity = git_email
                            except Exception as e:
                                logger.debug(f"Failed to fetch git config user.email: {e}")

                        # Final fallback to name or login if NO email found anywhere
                        if not identity:
                            identity = std_data.get("name") or std_data.get("login")
                except Exception as e:
                    logger.debug(f"GitHub /user identity fetch failed: {e}")

            # Fallback: local gh config (only if identity still None)
            if not identity:
                gh_config_path = os.path.expanduser("~/.config/gh/hosts.yml")

                def _read_gh_identity(path: str) -> str | None:
                    if not os.path.exists(path):
                        return None
                    import yaml

                    with open(path) as f:
                        config = yaml.safe_load(f) or {}
                    host_config = config.get("github.com", {})
                    return host_config.get("user") or next(iter(host_config.get("users", {})), None)

                try:
                    identity = await asyncio.to_thread(_read_gh_identity, gh_config_path)
                except Exception:
                    logger.debug("Failed to read GitHub identity from config", exc_info=True)

            if identity:
                self._identity = identity
                # Only update label if it's currently NOT set, empty string, or the placeholder 'Default'
                # This allows clearing the field in settings to revert to auto-discovery.
                if not self.account_label or self.account_label.lower() == "default":
                    self.account_label = identity

                if self.account_id:
                    # Update metadata in cache so it can be used for label fallbacks.
                    # Note: We only push to cache if we don't have a specific override already
                    # active, to prevent "real" names from clobbering preferred ones in the cache.
                    asyncio.create_task(
                        token_cache.update_account_metadata(
                            "github", self.account_id, name=self.account_label
                        )
                    )
            cards = self._parse_api_responses(user_resp, token_resp, user_data)
            logger.info(
                "GitHub collector: API calls complete — user_resp=%s token_resp=%s cards=%d",
                user_resp.status_code,
                token_resp.status_code if token_resp else "skipped",
                len(cards),
            )
            # Cache results (including empty/error cards) to avoid hammering API
            self._cached_results = cards
            self._last_fetch = now
            return cards
        except Exception as e:
            logger.warning(
                "GitHub Copilot API strategy failed: %s — %s",
                type(e).__name__,
                e,
                exc_info=True,
            )
            err = error_card(
                "GitHub Copilot",
                "🐙",
                f"{type(e).__name__}: {str(e)[:60]}",
                error_type="api_error",
            )
            self._cached_results = [err]
            self._last_fetch = now
            return [err]

    async def reset(self):
        """Reset internal collector state and cache."""
        self._cached_results = None
        self._last_fetch = None
        logger.info("GitHubCollector internal cache cleared.")

    async def _get_token(self) -> str | None:
        """Internal helper to get token from multiple sources."""
        # Check standard credentials
        creds = credential_provider.get_github_data()
        token = creds.get("api_key")

        # If we have email/name in creds, cache them as identity
        if token:
            self._current_input_source = getattr(creds, "sources", {}).get("api_key", "server")
            identity = creds.get("email") or creds.get("name")
            if identity:
                self._identity = identity
                if not self.account_label or self.account_label.lower() == "default":
                    self.account_label = identity
            return token

        # Check account-specific token cache
        if self.account_id:
            cache_data = await token_cache.get_with_metadata("github", account_id=self.account_id)
            if cache_data:
                tokens, metadata = cache_data
                source = metadata.get("source") or "sidecar"
                self._current_input_source = (
                    "config" if source in ("config", "manual_config") else "sidecar"
                )
                return tokens.get("api_key")
        return None

    def _parse_api_responses(self, user_resp, token_resp, user_data) -> list[dict[str, Any]]:
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

    # Human-readable labels for known quota keys.  Keys not listed here fall
    # back to key.replace("_", " ").title() so new GitHub fields surface automatically.
    _QUOTA_DISPLAY_NAMES: dict[str, str] = {
        "completions": "Completions",
        "chat": "Chat",
        "included_credits": "Included Credits",
        "premium_interactions": "Premium Interactions",
    }

    def _parse_limited_quotas(
        self, data: dict[str, Any], detail_context: str
    ) -> list[dict[str, Any]]:
        """Parse limited_user_quotas structure."""
        results = []
        quotas = data["limited_user_quotas"]
        monthly = data.get("monthly_quotas", {})
        reset_date = data.get("limited_user_reset_date")
        reset_at = None
        if reset_date:
            try:
                reset_at = parse_iso8601_utc(reset_date)
            except (ValueError, TypeError) as e:
                logger.debug(f"Could not parse GitHub reset date '{reset_date}': {e}")

        # Suppress identity suffix if label already carries the identity info.
        identity_suffix = ""
        if self._identity:
            if (
                not self.account_label
                or self.account_label.lower() == "default"
                or self.account_label == self._identity
            ):
                identity_suffix = f" · {self._identity}"

        for key, val in quotas.items():
            if not isinstance(val, int | float):
                continue
            display = self._QUOTA_DISPLAY_NAMES.get(key, key.replace("_", " ").title())
            monthly_val = monthly.get(key, 100)
            used_val = monthly_val - val if isinstance(monthly_val, int) else 0
            pct_used = (
                (used_val / monthly_val * 100)
                if isinstance(monthly_val, int | float) and monthly_val > 0
                else 0
            )
            pace = PaceCalculator.estimate_longevity(pct_used, reset_at)
            results.append(
                {
                    "service_name": "Copilot",
                    "variant": display,
                    "icon": "🐙",
                    "remaining": f"{val:,}",
                    "unit": (f"/ {monthly_val:,}" if isinstance(monthly_val, int) else "remaining"),
                    "reset": reset_at.isoformat() if reset_at else None,
                    "health": HealthCalculator.from_remaining(val, monthly_val)
                    if isinstance(monthly_val, int | float)
                    else "warning",
                    "pace": pace,
                    "detail": f"{val}/{monthly_val if isinstance(monthly_val, int) else '??'} requests left {detail_context}{identity_suffix}",
                    "used_value": float(used_val),
                    "limit_value": float(monthly_val)
                    if isinstance(monthly_val, int | float)
                    else 100.0,
                    "is_unlimited": False,
                    "tier": "free",
                    "unit_type": "requests",
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": self.DATA_SOURCE_API,
                    "input_source": getattr(self, "_current_input_source", "unknown"),
                    "usage_url": "https://github.com/settings/copilot/features",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
        return results

    def _parse_quota_snapshots(
        self, snapshots: list[dict] | dict, plan: str
    ) -> list[dict[str, Any]]:
        """Parse quota_snapshots structure.

        GitHub changed this field from a list of dicts (each with a "metric" key)
        to a dict keyed by metric name.  Normalise to a list before processing so
        both shapes work.
        """
        # Normalise new dict format: {"chat": {...}, "completions": {...}}
        if isinstance(snapshots, dict):
            snapshots = [{"metric": k, **v} for k, v in snapshots.items()]

        results = []
        metric_map = {
            "premium_interactions": "Premium Interactions",
            "chat": "Chat",
            "completions": "Autocomplete",
            "included_credits": "Included Credits",
        }
        tier_map = {"individual": "pro", "business": "team", "enterprise": "enterprise"}
        tier_name = tier_map.get(plan.lower(), plan.lower()) if plan else None

        for snap in snapshots:
            # Skip metrics not available on this plan (has_quota absent → assume present)
            if not snap.get("has_quota", True):
                continue

            metric_raw = snap.get("metric", "unknown")
            metric = metric_map.get(metric_raw, metric_raw.replace("_", " ").title())
            rem = snap.get("remaining")
            ent = snap.get("entitlement")

            if rem is not None and ent is not None and ent > 0:
                used_val = ent - rem
                pct_used = (used_val / ent * 100) if ent > 0 else 0
                pace = PaceCalculator.estimate_longevity(pct_used, None)
                results.append(
                    {
                        "service_name": "Copilot",
                        "variant": metric,
                        "window_type": "rolling",
                        "icon": "🐙",
                        "remaining": f"{rem:,}",
                        "unit": f"/ {ent:,}",
                        "reset": "Rolling",
                        "health": HealthCalculator.from_remaining(rem, ent),
                        "pace": pace,
                        "detail": f"{pct_used:.1f}% used • {plan} [Pro Tier]",
                        "used_value": float(used_val),
                        "limit_value": float(ent),
                        "is_unlimited": snap.get("unlimited", False),
                        "tier": tier_name,
                        "unit_type": "requests",
                        "reset_at": None,
                        "data_source": self.DATA_SOURCE_API,
                        "input_source": getattr(self, "_current_input_source", "unknown"),
                        "usage_url": "https://github.com/settings/copilot/features",
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )
        return results
