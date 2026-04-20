"""
zAI collector for quota limits.

Uses API key (ZAI_API_KEY) to fetch quota limits (tokens/time) from:
- api.z.ai (global, default)
- open.bigmodel.cn (China, fallback)

Reference: CodexBar only uses quota endpoint, no balance.

Token sources (fallback order):
1. Config token (UI-set via provider_id="zai")
2. Environment variable ZAI_API_KEY

Environment overrides:
- ZAI_API_HOST: Override host (e.g., "open.bigmodel.cn" for China)
- ZAI_QUOTA_URL: Override full quota URL
"""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import HealthCalculator, human_delta
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider

logger = logging.getLogger(__name__)


class ZaiCollector(BaseCollector):
    """Collector for zAI (Zhipu AI/GLM) quota limits."""

    PROVIDER_ID = "zai"
    DEFAULT_WINDOW_TYPE = "monthly"

    MILLISECOND_TIMESTAMP_THRESHOLD = 1_000_000_000_000

    QUOTA_PATH = "api/monitor/usage/quota/limit"

    DEFAULT_ENDPOINTS = [
        "https://api.z.ai/api/monitor/usage/quota/limit",
        "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
    ]

    async def _get_api_key(self) -> str | None:
        """DB (UI-set via provider_id='zai') → env var."""
        key = credential_provider.get_provider_api_key("zai") or settings.ZAI_API_KEY or None
        if key:
            self._current_input_source = "server"
            return key

        if self.account_id:
            # Check account-specific token cache
            from app.services.token_cache import token_cache

            cache_data = await token_cache.get_with_metadata("zai", account_id=self.account_id)
            if cache_data:
                tokens, metadata = cache_data
                source = metadata.get("source") or "sidecar"
                self._current_input_source = "manual" if source == "manual_config" else "sidecar"
                return tokens.get("api_key")
        return None

    async def _get_current_key(self) -> str | None:
        """Async version of _get_api_key that handles cache metadata."""
        return await self._get_api_key()

    async def is_configured(self) -> bool:
        """Check if zAI API key is present."""
        return self._is_valid_credential(await self._get_current_key())

    def _get_quota_endpoints(self) -> list[str]:
        """Resolve quota endpoints with env overrides."""
        env_host = settings.ZAI_API_HOST or None
        env_url = settings.ZAI_QUOTA_URL or None

        if env_url:
            return [env_url]

        if env_host:
            if not env_host.startswith(("http://", "https://")):
                env_host = f"https://{env_host}"
            return [f"{env_host.rstrip('/')}/{self.QUOTA_PATH}"]

        return self.DEFAULT_ENDPOINTS

    def _fallback_strategies(self) -> list[Any]:
        return []

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect quota limits."""
        key = await self._get_current_key()
        if not key or key.lower() == "zai":
            return []

        endpoints = self._get_quota_endpoints()
        quota_cards = await self._fetch_quota(client, key, endpoints)
        return quota_cards if quota_cards else []

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return error card when all collection fails."""
        key = await self._get_current_key()
        if not key or key.lower() == "zai":
            return [self._info_card("No API key configured", "Missing/Invalid Key")]
        return [self._info_card("API unavailable", "Check network or quota")]

    def _info_card(self, remaining: str, detail: str) -> dict[str, Any]:
        """Build an info card when no quota data is available."""
        return {
            "service_name": "zAI Plan",
            "icon": "🌐",
            "remaining": remaining,
            "unit": "quota",
            "reset": "—",
            "health": "good",
            "pace": "N/A",
            "detail": detail,
            "data_source": self.DATA_SOURCE_API,
            "input_source": getattr(self, "_current_input_source", "unknown"),
            "is_unlimited": False,
            "unit_type": "unknown",
        }

    async def _fetch_quota(
        self, client: httpx.AsyncClient, key: str, endpoints: list[str]
    ) -> list[dict[str, Any]] | None:
        """Fetch quota limits trying multiple endpoints."""
        for endpoint in endpoints:
            try:
                result = await self._fetch_quota_from_endpoint(client, key, endpoint)
                if result:
                    return result
            except Exception as e:
                logger.debug(f"zAI quota endpoint {endpoint} failed: {e}")
                continue
        return None

    async def _fetch_quota_from_endpoint(
        self, client: httpx.AsyncClient, key: str, endpoint: str
    ) -> list[dict[str, Any]] | None:
        """Fetch quota from a specific endpoint."""
        resp = await client.get(
            endpoint,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=10.0,
        )

        if resp.status_code != 200:
            logger.debug(f"zAI quota fetch failed at {endpoint}: HTTP {resp.status_code}")
            return None

        data = resp.json()

        code = data.get("code", 0)
        success = data.get("success", False)
        if not (success and code == 200):
            logger.debug(
                f"zAI quota response invalid at {endpoint}: code={code}, success={success}"
            )
            return None

        plan_data = data.get("data", {})
        plan_name = (
            plan_data.get("planName") or plan_data.get("plan") or plan_data.get("packageName")
        )
        limits = plan_data.get("limits", [])

        if not limits:
            if plan_name:
                return [self._info_card("No active plan", f"Plan: {plan_name}")]
            return [self._info_card("No active plan", "No quota limits available")]

        cards = []
        for limit in limits:
            card = self._parse_limit(limit, plan_name or "Unknown")
            if card:
                cards.append(card)

        return cards if cards else None

    def _parse_limit(self, limit: dict[str, Any], plan_name: str) -> dict[str, Any] | None:
        """Parse a single limit entry into a card."""
        limit_type = limit.get("type", "")

        limit_val = limit.get("usage", 0)
        used_val = limit.get("currentValue", 0)
        remaining_val = limit.get("remaining")

        if remaining_val is not None:
            remaining = remaining_val
        else:
            remaining = max(0, limit_val - used_val) if limit_val else 0

        pct_used = (used_val / limit_val * 100) if limit_val > 0 else 0
        reset_ts = limit.get("nextResetTime")

        unit_val = limit.get("number", 0)
        unit_type = limit.get("unit", 0)

        window_str = self._format_window(unit_type, unit_val)

        if limit_type == "TOKENS_LIMIT":
            service = "zAI Plan (Tokens)"
            remaining_str = f"{remaining:,}" if remaining else "0"
            unit = f"{limit_val:,}" if limit_val else "0"
            detail = f"{used_val:,} used · {plan_name}"
            unit_type_str = "tokens"
        elif limit_type == "TIME_LIMIT":
            service = "zAI Plan (Time)"
            remaining_str = f"{remaining}" if remaining is not None else "0"
            unit = f"{limit_val} min" if limit_val else "0 min"
            detail = f"{used_val} min used · {plan_name}"
            unit_type_str = "minutes"
        else:
            service = f"zAI Plan ({limit_type})"
            remaining_str = f"{remaining}" if remaining is not None else "0"
            unit = f"{limit_val}" if limit_val else "0"
            detail = f"{used_val} used · {plan_name}"
            unit_type_str = "generic"

        reset_str = "Manual"
        reset_at = None
        if reset_ts:
            try:
                if reset_ts > self.MILLISECOND_TIMESTAMP_THRESHOLD:
                    reset_ts = reset_ts / 1000
                reset_dt = datetime.fromtimestamp(reset_ts, tz=UTC)
                reset_str = human_delta(reset_dt)
                reset_at = reset_dt.isoformat()
            except (ValueError, OSError, OverflowError):
                reset_str = "Unknown"

        health = HealthCalculator.from_percentage(pct_used)
        pace = "Stable" if health == "good" else "High" if health == "warning" else "Critical"

        return {
            "service_name": service,
            "icon": "🌐",
            "remaining": remaining_str,
            "unit": unit,
            "reset": reset_str,
            "health": health,
            "pace": pace,
            "detail": detail,
            "used_value": float(used_val),
            "limit_value": float(limit_val),
            "is_unlimited": False,
            "unit_type": unit_type_str,
            "reset_at": reset_at,
            "window": window_str,
            "data_source": self.DATA_SOURCE_API,
            "input_source": getattr(self, "_current_input_source", "unknown"),
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def _format_window(self, unit: int, number: int) -> str | None:
        """Format window duration from unit + number."""
        if not number:
            return None

        unit_map = {
            1: ("day", "days"),
            3: ("hour", "hours"),
            5: ("minute", "minutes"),
            6: ("week", "weeks"),
        }
        singular, plural = unit_map.get(unit, ("unit", "units"))
        label = singular if number == 1 else plural

        return f"{number} {label}"
