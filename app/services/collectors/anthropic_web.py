"""
Anthropic (Claude) Web API collector and Statusline bridge.

Handles:
- Statusline JSON bridge (fast local path)
- Web API collection via Chrome sessionKey cookie
- Response parsing for both sources
"""

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.browser_cookies import get_claude_session_cookie
from app.core.config import is_local_collector_enabled, settings
from app.core.utils import HealthCalculator, PaceCalculator, http_request_with_retry, human_delta

logger = logging.getLogger(__name__)


class AnthropicWebMixin:
    """
    Mixin providing Statusline and Web API collection for Anthropic (Claude).
    Intended to be composed into AnthropicCollector.
    """

    _name_map = {
        "five_hour": "Session Window",
        "seven_day": "Weekly Window",
        "seven_day_sonnet": "Sonnet Weekly",
        "seven_day_opus": "Opus Weekly",
        "extra_usage": "Extra Usage",
    }

    # ─────────────────────────────── Statusline (fast path) ──────────────────

    async def _strategy_statusline(self) -> list[dict[str, Any]]:
        """
        Choice #1: Read the local Claude statusline file (Fast Path).
        Returns metrics if the file exists and is fresh (< 5 mins old).
        """
        if not is_local_collector_enabled():
            return []

        # Try multiple potential paths
        home = os.path.expanduser("~")
        paths = [
            settings.CLAUDE_STATUSLINE_PATH,
            os.path.join(home, ".claude", "statusline.json"),
            os.path.join(home, "Library", "Application Support", "Claude", "statusline.json"),
        ]

        path = None
        for p in paths:
            if os.path.exists(p):
                path = p
                break

        if not path:
            return []

        try:
            # Freshness check (5 minutes)
            mtime = os.path.getmtime(path)
            if (time.time() - mtime) > 300:
                # logger.info(f"Claude statusline file is stale ({int(time.time() - mtime)}s old) at {path}")
                return []

            with open(path) as f:
                data = json.load(f)

            self._last_statusline_data = data

            # Extract identity from local credentials to ensure account_label is set
            identity_str = ""
            creds = None
            if hasattr(self, "_get_credentials") and hasattr(self, "_extract_identity_from_oauth"):
                creds = await self._get_credentials()
                identity_str = self._extract_identity_from_oauth(creds)

            identity_suffix = f" | {identity_str}" if identity_str else ""

            return self._parse_statusline_response(data, identity_suffix, creds)
        except Exception as e:
            logger.debug(f"Failed to read Claude statusline: {e}")
            return []

    def _parse_statusline_response(
        self, data: dict[str, Any], identity_suffix: str = "", creds: dict | None = None
    ) -> list[dict[str, Any]]:
        """Parse statusline.json into standardized quota cards."""
        results = []
        now = datetime.now(UTC)

        # Extract identity from suffix (strip " | ")
        identity_str = identity_suffix.lstrip(" |") if identity_suffix else ""

        tier = None
        if creds:
            raw_tier = creds.get("claudeAiOauth", {}).get("rateLimitTier")
            if raw_tier:
                tier_map = {
                    "tier_0": "Free",
                    "tier_1": "Pro",
                    "tier_2": "Max",
                    "tier_3": "Team",
                    "tier_4": "Enterprise",
                    "tier_5": "Enterprise",
                }
                tier = tier_map.get(raw_tier.lower(), raw_tier.capitalize())
        if not tier and hasattr(self, "_get_local_config_hints"):
            local_hints = self._get_local_config_hints()
            local_tier = local_hints.get("billing_tier") or local_hints.get("tier")
            if local_tier:
                tier = str(local_tier).capitalize()

        # 1. Rate Limits
        limits = data.get("rate_limits", {})
        for key, info in limits.items():
            u_type = self._name_map.get(key, key.replace("_", " ").title())
            pct_used = float(info.get("used_percentage", 0.0))
            reset_ts = info.get("resets_at")
            reset_at = datetime.fromtimestamp(reset_ts, tz=UTC) if reset_ts else None

            # Correct window type based on key
            w_type = (
                "session" if key == "five_hour" else "weekly" if "seven_day" in key else "unknown"
            )

            results.append(
                {
                    "service_name": f"Claude ({u_type})",
                    "icon": "🟠",
                    "remaining": f"{(100 - pct_used):.1f}%",
                    "unit": "capacity",
                    "reset": human_delta(reset_at),
                    "health": HealthCalculator.from_percentage(pct_used),
                    "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                    "detail": f"{pct_used:.1f}% used [Statusline]{identity_suffix}",
                    "used_value": pct_used,
                    "limit_value": 100.0,
                    "unit_type": "percent",
                    "window_type": w_type,
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": "statusline",
                    "tier": tier,
                    "account_label": identity_str,
                    "updated_at": now.isoformat(),
                }
            )

        # 2. Session Context (Tokens/Cost)
        context = data.get("context_window", {})
        if context:
            input_tokens = context.get("total_input_tokens", 0)
            output_tokens = context.get("total_output_tokens", 0)
            total = input_tokens + output_tokens
            max_tokens = context.get("max_tokens", 200000)

            results.append(
                {
                    "service_name": "Claude (Session Tokens)",
                    "icon": "🪙",
                    "remaining": f"{total:,}",
                    "unit": f"/ {max_tokens:,}",
                    "reset": data.get("model", {}).get("display_name", "Sonnet"),
                    "health": "good",
                    "pace": "Active",
                    "detail": f"IN: {input_tokens:,} | OUT: {output_tokens:,} [Statusline]{identity_suffix}",
                    "used_value": float(total),
                    "limit_value": float(max_tokens),
                    "unit_type": "tokens",
                    "window_type": "session",
                    "data_source": "statusline",
                    "tier": tier,
                    "account_label": identity_str,
                    "updated_at": now.isoformat(),
                }
            )

        cost = data.get("cost", {})
        if cost and cost.get("total_cost_usd", 0) > 0:
            total_cost = cost.get("total_cost_usd")
            results.append(
                {
                    "service_name": "Claude (Session Cost)",
                    "icon": "💰",
                    "remaining": f"${total_cost:.2f}",
                    "unit": "USD",
                    "reset": "This Session",
                    "health": "good",
                    "pace": "Stable",
                    "detail": f"+{cost.get('total_lines_added', 0)} / -{cost.get('total_lines_deleted', 0)} lines [Statusline]{identity_suffix}",
                    "window_type": "session",
                    "data_source": "statusline",
                    "tier": tier,
                    "account_label": identity_str,
                    "updated_at": now.isoformat(),
                }
            )

        return results

    # ─────────────────────────────── Web API (cookie path) ───────────────────

    async def _has_web_cookie(self) -> bool:
        """Check if a web cookie is available without making API calls."""
        return await asyncio.to_thread(get_claude_session_cookie) is not None

    async def _get_claude_via_web_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """
        Secondary strategy: Fetch Claude quota via Web API using Chrome cookies.

        Extracts the sessionKey cookie from Chrome and calls Claude's web API
        endpoints. Provides the same data quality as OAuth but without a token.

        Endpoints called:
        1. GET /api/organizations — Get organization UUID
        2. GET /api/account — Get tier/plan info (optional)
        3. GET /api/organizations/{orgId}/usage — Get usage quotas
        """
        session_key = await asyncio.to_thread(get_claude_session_cookie)
        if not session_key:
            logger.debug("No Claude sessionKey cookie found in Chrome")
            return []

        headers = {
            "Cookie": f"sessionKey={session_key}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Referer": "https://claude.ai/chat/",
            "Accept": "application/json",
        }

        try:
            # Step 1: Get organization ID
            orgs_resp = await http_request_with_retry(
                client, "GET", "https://claude.ai/api/organizations", headers=headers, timeout=10.0
            )
            if orgs_resp.status_code != 200:
                logger.debug(f"Claude Web API orgs call failed: {orgs_resp.status_code}")
                return []

            orgs_data = orgs_resp.json()
            if not orgs_data or not isinstance(orgs_data, list) or len(orgs_data) == 0:
                logger.debug("No organizations found in Claude Web API response")
                return []

            org = orgs_data[0]
            org_id = org.get("uuid") or org.get("id")
            if not org_id:
                logger.debug("No organization UUID found in response")
                return []

            # Step 2: Get account info for tier/plan
            account_data = None
            try:
                account_resp = await http_request_with_retry(
                    client, "GET", "https://claude.ai/api/account", headers=headers, timeout=10.0
                )
                if account_resp.status_code == 200:
                    account_data = account_resp.json()
            except Exception as e:
                logger.debug(f"Could not fetch account info: {e}")

            # Step 3: Get usage data
            usage_resp = await http_request_with_retry(
                client,
                "GET",
                f"https://claude.ai/api/organizations/{org_id}/usage",
                headers=headers,
                timeout=10.0,
            )
            if usage_resp.status_code != 200:
                logger.debug(f"Claude Web API usage call failed: {usage_resp.status_code}")
                return []

            usage_data = usage_resp.json()
            return self._parse_web_api_response(usage_data, org, account_data)

        except httpx.HTTPError as e:
            logger.debug(f"Claude Web API HTTP error: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.debug(f"Claude Web API JSON decode error: {e}")
            return []
        except Exception as e:
            logger.error(f"Claude Web API collection failed: {e}")
            return []

    def _extract_identity_from_web(self, org_data: dict[str, Any]) -> str:
        """Extract account identity string from Web API organization response."""
        membership = org_data.get("membership", {})
        user = membership.get("user", {})
        email = user.get("email", "")
        org_name = org_data.get("name", "")

        if email and org_name:
            return f"{email} @ {org_name}"
        if email:
            return email
        if org_name:
            return f"org: {org_name}"
        return ""

    def _parse_web_api_response(
        self,
        data: dict[str, Any],
        org_data: dict[str, Any] | None = None,
        account_data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Parse Web API response into standardized quota cards."""
        results = []

        identity_str = self._extract_identity_from_web(org_data) if org_data else ""
        identity_suffix = f" | {identity_str}" if identity_str else ""

        # Try multiple sources for tier — account API, org data, then give up
        plan = ""
        if account_data:
            plan = (
                account_data.get("plan")
                or account_data.get("account_type")
                or account_data.get("subscription")
                or ""
            )
        if not plan and org_data:
            # Org response sometimes has plan/capabilities info
            plan = (
                org_data.get("plan")
                or org_data.get("subscription")
                or org_data.get("account_type")
                or org_data.get("membership", {}).get("billing_type")
                or ""
            )
        tier = plan.capitalize() if plan else None

        # All four core windows — show even if API returns null (mirrors OAuth path behaviour)
        window_map = {
            "five_hour": "Session Window",
            "seven_day": "Weekly Window",
            "seven_day_sonnet": "Sonnet Weekly",
            "seven_day_opus": "Opus Weekly",
        }

        for api_key, display_name in window_map.items():
            window_data = data.get(api_key)
            # If key is absent entirely, synthesize a null entry so the window still appears
            if window_data is None:
                window_data = {"utilization": None, "resetsAt": None}
            if not isinstance(window_data, dict):
                continue

            # Web API uses "utilization" (0.0 to 1.0) or "percentUsed" (0 to 100)
            raw_util = window_data.get("utilization")
            if raw_util is not None:
                pct_used = float(raw_util) * 100.0
            else:
                raw_pct = window_data.get("percentUsed")
                pct_used = float(raw_pct) if raw_pct is not None else 0.0

            remaining_pct = 100.0 - pct_used

            reset_at = None
            reset_raw = window_data.get("resetsAt")
            if reset_raw:
                try:
                    reset_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            # Correct window type based on key
            w_type = (
                "session"
                if api_key == "five_hour"
                else "weekly"
                if "seven_day" in api_key
                else "unknown"
            )

            results.append(
                {
                    "service_name": f"Claude ({display_name})",
                    "icon": "🟠",
                    "remaining": f"{remaining_pct:.1f}%",
                    "unit": "capacity",
                    "reset": human_delta(reset_at),
                    "health": HealthCalculator.from_percentage(pct_used),
                    "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                    "detail": f"{pct_used:.1f}% used [Web API]{identity_suffix}",
                    "used_value": pct_used,
                    "limit_value": 100.0,
                    "is_unlimited": False,
                    "unit_type": "percent",
                    "window_type": w_type,
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": "web_api",
                    "tier": tier,
                    "account_label": identity_str,
                    "usage_url": "https://claude.ai/settings/usage",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )

        # 2. Balance / Prepaid Credits (New)
        balance_keys = ["current_balance", "available_balance", "balance", "credits"]
        for b_key in balance_keys:
            bal_val = data.get(b_key)
            if bal_val is not None:
                try:
                    bal = float(bal_val)
                    results.append(
                        {
                            "service_name": "Claude (Current Balance)",
                            "icon": "💰",
                            "remaining": f"${bal:.2f}",
                            "unit": "USD",
                            "reset": "Prepaid",
                            "health": HealthCalculator.from_balance(bal),
                            "pace": "Manual Top-up",
                            "detail": f"Credits: ${bal:.2f} [Web API]{identity_suffix}",
                            "used_value": 0.0,
                            "limit_value": bal,
                            "unit_type": "currency",
                            "window_type": "prepaid",
                            "data_source": "web_api",
                            "tier": tier,
                            "account_label": identity_str,
                            "usage_url": "https://claude.ai/settings/usage",
                            "updated_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    # Found a balance, break to avoid duplicates if multiple keys exist
                    break
                except (ValueError, TypeError):
                    pass

        # 3. Extra usage / overage (Fixed formatting)
        extra_data = data.get("extra_usage") or data.get("overage")
        if extra_data and isinstance(extra_data, dict):
            raw_spend = extra_data.get("spend")
            raw_limit = extra_data.get("limit")
            spend = float(raw_spend) if raw_spend is not None else 0.0
            limit = float(raw_limit) if raw_limit is not None else 0.0

            if limit > 0:
                remaining = max(0.0, limit - spend)
                results.append(
                    {
                        "service_name": "Claude (Extra Usage)",
                        "icon": "💰",
                        "remaining": f"${remaining:.2f}",
                        "unit": "USD",
                        "reset": "Monthly",
                        "health": HealthCalculator.from_spend(spend, limit),
                        "pace": "Sustainable",
                        "detail": f"${spend:.2f} / ${limit:.2f} [Web API]{identity_suffix}",
                        "used_value": spend,
                        "limit_value": limit,
                        "unit_type": "currency",
                        "window_type": "monthly",
                        "data_source": "web_api",
                        "tier": tier,
                        "account_label": identity_str,
                        "usage_url": "https://claude.ai/settings/usage",
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )

        return results
