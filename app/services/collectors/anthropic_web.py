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
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.browser_cookies import get_claude_session_cookie
from app.core.config import is_local_collector_enabled, settings
from app.core.utils import HealthCalculator, PaceCalculator, http_request_with_retry, human_delta
from app.services.collectors._anthropic_common import (
    ANTHROPIC_WINDOW_NAME_MAP,
    anthropic_model_id_for,
    classify_anthropic_window_type,
)
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

# Holds references to fire-and-forget tasks so the GC doesn't cancel them mid-run.
_pending_tasks: set[asyncio.Task] = set()


class AnthropicWebMixin:
    """
    Mixin providing Statusline and Web API collection for Anthropic (Claude).
    Intended to be composed into AnthropicCollector.
    """

    _name_map = ANTHROPIC_WINDOW_NAME_MAP

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
                # Match pro/max/team/free followed by optional multiplier like 5x or 20x
                match = re.search(r"(pro|max|team|free)[\s_]*(\d+x)?", raw_tier.lower())
                if match:
                    base = match.group(1).capitalize()
                    mult = match.group(2)
                    tier = f"{base} {mult}" if mult else base
                else:
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
            pct_used = float(info.get("used_percentage", 0.0))
            reset_ts = info.get("resets_at")
            reset_at = datetime.fromtimestamp(reset_ts, tz=UTC) if reset_ts else None

            w_type = classify_anthropic_window_type(key)

            results.append(
                {
                    "service_name": "Claude",
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
                    "model_id": anthropic_model_id_for(key),
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": self.DATA_SOURCE_LOCAL,
                    "input_source": "server",
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
                    "service_name": "Claude",
                    "variant": "Tokens",
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
                    "model_id": None,
                    "data_source": self.DATA_SOURCE_LOCAL,
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
                    "service_name": "Claude",
                    "variant": "Cost",
                    "icon": "💰",
                    "remaining": f"${total_cost:.2f}",
                    "unit": "USD",
                    "reset": "This Session",
                    "health": "good",
                    "pace": "Stable",
                    "detail": f"+{cost.get('total_lines_added', 0)} / -{cost.get('total_lines_deleted', 0)} lines [Statusline]{identity_suffix}",
                    "window_type": "session",
                    "model_id": None,
                    "data_source": self.DATA_SOURCE_LOCAL,
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
            # Fallback to token cache (user-provided in UI or sidecar-ingested)
            session_key = await token_cache.get_token(
                "anthropic", "cookie_sessionKey", account_id=self.account_id or "default"
            )
            # Second fallback to generic UI-provided session_cookie
            if not session_key:
                session_key = await token_cache.get_token(
                    "anthropic", "session_cookie", account_id=self.account_id or "default"
                )

            # Third fallback: Check if a session key was accidentally put into the oauth_token field
            if not session_key:
                oauth_token = await token_cache.get_token(
                    "anthropic", "oauth_token", account_id=self.account_id or "default"
                )
                if oauth_token and oauth_token.startswith("sk-ant-sid"):
                    session_key = oauth_token

        if not session_key:
            logger.debug("No Claude sessionKey cookie found (browser or cache)")
            return []

        # If the session_key already looks like a multi-cookie string (contains ; or =),
        # use it as-is. Otherwise wrap it in sessionKey=.
        cookie_header = (
            session_key if ";" in session_key or "=" in session_key else f"sessionKey={session_key}"
        )

        # Extract activitySessionId from cookies if present for x-activity-session-id header
        import re

        activity_sid_match = re.search(r"activitySessionId=([^;]+)", cookie_header)
        activity_sid = activity_sid_match.group(1) if activity_sid_match else None

        headers = {
            "Cookie": cookie_header,
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "referer": "https://claude.ai/settings/usage",
            "origin": "https://claude.ai",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-requested-with": "XMLHttpRequest",
        }
        if activity_sid:
            headers["x-activity-session-id"] = activity_sid

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
                    raw_acc = account_resp.json()
                    # Handle both single object and list response
                    account_data = (
                        raw_acc[0] if isinstance(raw_acc, list) and len(raw_acc) > 0 else raw_acc
                    )
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

    def _extract_identity_from_web(
        self, org_data: dict[str, Any], account_data: dict[str, Any] | None = None
    ) -> str:
        """Extract account identity string from Web API organization/account response."""
        email = ""
        if account_data:
            email = account_data.get("email_address", "")

        if not email and org_data:
            membership = org_data.get("membership", {})
            user = membership.get("user", {})
            email = user.get("email", "")

        org_name = org_data.get("name", "") if org_data else ""

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

        identity_str = (
            self._extract_identity_from_web(org_data, account_data)
            if org_data or account_data
            else ""
        )
        identity_suffix = f" | {identity_str}" if identity_str else ""

        # Identity Promotion: sync discovered email/name back to the token cache metadata
        if identity_str and hasattr(self, "account_id") and self.account_id:
            task = asyncio.create_task(
                token_cache.update_account_metadata("anthropic", self.account_id, name=identity_str)
            )
            _pending_tasks.add(task)
            task.add_done_callback(_pending_tasks.discard)
            self.account_label = identity_str

        # Tier discovery - use regex to extract pro/max/team and multiplier (e.g. 5x)
        # Check both account_data and org_data for rate_limit_tier
        tier = None
        raw_tier = None
        if account_data:
            raw_tier = account_data.get("rate_limit_tier") or account_data.get("rate_tier_limit")
        if not raw_tier and org_data:
            raw_tier = org_data.get("rate_limit_tier") or org_data.get("rate_tier_limit")

        if raw_tier:
            # Match pro/max/team/free followed by optional multiplier like 5x or 20x
            match = re.search(r"(pro|max|team|free)[\s_]*(\d+x)?", raw_tier.lower())
            if match:
                base = match.group(1).capitalize()
                mult = match.group(2)
                tier = f"{base} {mult}" if mult else base

        if not tier:
            # Fallback to older plan detection fields
            plan = ""
            if account_data:
                plan = (
                    account_data.get("plan")
                    or account_data.get("account_type")
                    or account_data.get("subscription")
                    or ""
                )
            if not plan and org_data:
                plan = (
                    org_data.get("plan")
                    or org_data.get("subscription")
                    or org_data.get("account_type")
                    or org_data.get("membership", {}).get("billing_type")
                    or ""
                )
            tier = plan.capitalize() if plan else None

        # extra_usage has a different shape and is processed separately as extra_data below
        window_map = {k: v for k, v in ANTHROPIC_WINDOW_NAME_MAP.items() if k != "extra_usage"}

        # All known windows — we start with these to ensure order
        all_keys = list(window_map.keys())
        for k in data:
            if k not in all_keys and k not in (
                "account",
                "organization",
                "billing",
                "current_balance",
                "available_balance",
                "balance",
                "credits",
                "extra_usage",
                "overage",
            ):
                all_keys.append(k)

        # Initialize here so balance/extra_data sections below can reference it even when
        # the windows loop produces no results (all window_data keys are None).
        tier_label = f" [{tier}]" if tier else ""

        for api_key in all_keys:
            window_data = data.get(api_key)

            # Skip null results per user request (not active usage limits)
            if window_data is None:
                continue

            if not isinstance(window_data, dict):
                continue

            # Web API uses 0-100 scale directly (e.g. 1 means 1%)
            raw_util = window_data.get("utilization")
            if raw_util is not None:
                pct_used = float(raw_util)
            else:
                raw_pct = window_data.get("percentUsed")
                pct_used = float(raw_pct) if raw_pct is not None else 0.0

            remaining_pct = 100.0 - pct_used

            reset_at = None
            reset_raw = window_data.get("resets_at") or window_data.get("resetsAt")
            if reset_raw:
                try:
                    reset_at = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            w_type = classify_anthropic_window_type(api_key)
            service_name = "Claude Design" if api_key == "seven_day_omelette" else "Claude"

            results.append(
                {
                    "service_name": service_name,
                    "icon": "🟠",
                    "remaining": f"{remaining_pct:.1f}%",
                    "unit": "capacity",
                    "reset": human_delta(reset_at),
                    "health": HealthCalculator.from_percentage(pct_used),
                    "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                    "detail": f"{pct_used:.1f}% used{tier_label} [Web API]{identity_suffix}",
                    "used_value": pct_used,
                    "limit_value": 100.0,
                    "is_unlimited": False,
                    "unit_type": "percent",
                    "window_type": w_type,
                    "model_id": anthropic_model_id_for(api_key),
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": self.DATA_SOURCE_WEB,
                    "input_source": getattr(self, "_current_input_source", "unknown"),
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
                            "service_name": "Claude",
                            "variant": "Balance",
                            "icon": "💰",
                            "remaining": f"${bal:.2f}",
                            "unit": "USD",
                            "reset": "Prepaid",
                            "health": HealthCalculator.from_balance(bal),
                            "pace": "Manual Top-up",
                            "detail": f"Credits: ${bal:.2f}{tier_label} [Web API]{identity_suffix}",
                            "used_value": 0.0,
                            "limit_value": bal,
                            "unit_type": "currency",
                            "window_type": "rolling",
                            "data_source": self.DATA_SOURCE_WEB,
                            "input_source": getattr(self, "_current_input_source", "unknown"),
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

        # 3. Extra usage / overage (Support for credits and spend formats)
        extra_data = data.get("extra_usage") or data.get("overage")
        if extra_data and isinstance(extra_data, dict):
            # Check for new format: is_enabled, monthly_limit (in credits), used_credits
            is_enabled = extra_data.get("is_enabled", True)
            if is_enabled:
                raw_monthly_limit = extra_data.get("monthly_limit")
                raw_used_credits = extra_data.get("used_credits")
                currency_code = extra_data.get("currency", "USD")

                # Format currency symbol/prefix
                c_symbol = (
                    "€"
                    if currency_code == "EUR"
                    else "$"
                    if currency_code == "USD"
                    else f"{currency_code} "
                )

                if raw_monthly_limit is not None and raw_used_credits is not None:
                    # 1 credit = 0.01 currency
                    spend = float(raw_used_credits) * 0.01
                    limit = float(raw_monthly_limit) * 0.01
                    remaining = max(0.0, limit - spend)

                    results.append(
                        {
                            "service_name": "Claude",
                            "variant": "Extra Usage",
                            "icon": "💰",
                            "remaining": f"{c_symbol}{remaining:.2f}",
                            "unit": currency_code,
                            "reset": "Monthly",
                            "health": HealthCalculator.from_spend(spend, limit),
                            "pace": "Sustainable",
                            "detail": f"{c_symbol}{spend:.2f} / {c_symbol}{limit:.2f}{tier_label} [Web API]{identity_suffix}",
                            "used_value": spend,
                            "limit_value": limit,
                            "unit_type": "currency",
                            "currency": currency_code,
                            "window_type": "monthly",
                            "model_id": None,
                            "data_source": self.DATA_SOURCE_WEB,
                            "input_source": getattr(self, "_current_input_source", "unknown"),
                            "tier": tier,
                            "account_label": identity_str,
                            "usage_url": "https://claude.ai/settings/usage",
                            "updated_at": datetime.now(UTC).isoformat(),
                        }
                    )
                else:
                    # Fallback to older spend/limit format
                    raw_spend = extra_data.get("spend")
                    raw_limit = extra_data.get("limit")
                    if raw_limit is not None and float(raw_limit) > 0:
                        spend = float(raw_spend) if raw_spend is not None else 0.0
                        limit = float(raw_limit)
                        remaining = max(0.0, limit - spend)
                        results.append(
                            {
                                "service_name": "Claude",
                                "variant": "Extra Usage",
                                "icon": "💰",
                                "remaining": f"{c_symbol}{remaining:.2f}",
                                "unit": currency_code,
                                "reset": "Monthly",
                                "health": HealthCalculator.from_spend(spend, limit),
                                "pace": "Sustainable",
                                "detail": f"{c_symbol}{spend:.2f} / {c_symbol}{limit:.2f}{tier_label} [Web API]{identity_suffix}",
                                "used_value": spend,
                                "limit_value": limit,
                                "unit_type": "currency",
                                "currency": currency_code,
                                "window_type": "monthly",
                                "model_id": None,
                                "data_source": self.DATA_SOURCE_WEB,
                                "input_source": getattr(self, "_current_input_source", "unknown"),
                                "tier": tier,
                                "account_label": identity_str,
                                "usage_url": "https://claude.ai/settings/usage",
                                "updated_at": datetime.now(UTC).isoformat(),
                            }
                        )

        return results
