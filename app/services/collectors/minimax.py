import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import HealthCalculator, PaceCalculator, human_delta
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider

logger = logging.getLogger(__name__)


class MiniMaxCollector(BaseCollector):
    """
    Collector for MiniMax (Coding Plan) usage.

    Supports:
    - API token: https://api.minimax.io/v1/coding_plan/remains

    Config:
    - MINIMAX_API_KEY: API token (env var or DB)
    - MINIMAX_HOST: Override host (default: minimax.io, China: minimaxi.com) — a bare
      domain; both the API and dashboard URLs prefix it themselves.
    """

    PROVIDER_ID = "minimax"
    # No single canonical cadence — the plan has a 5h rolling window and a
    # weekly window, both stamped explicitly per card. Never "monthly".
    DEFAULT_WINDOW_TYPE = "unknown"
    USAGE_URL = "https://platform.minimax.io/user-center/payment/coding-plan"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self.api_key: str | None = None
        self.host: str = ""

    async def _get_current_creds(self) -> str | None:
        """Async credential retrieval with metadata support."""
        key = (
            credential_provider.get_provider_api_key("minimax") or settings.MINIMAX_API_KEY or None
        )

        if key:
            self._current_input_source = (
                "config" if credential_provider.get_provider_api_key("minimax") else "server"
            )
            return key

        if self.account_id:
            from app.services.token_cache import token_cache

            cache_data = await token_cache.get_with_metadata("minimax", account_id=self.account_id)
            if cache_data:
                tokens, metadata = cache_data
                source = metadata.get("source") or "sidecar"
                self._current_input_source = (
                    "config" if source in ("config", "manual_config") else "sidecar"
                )
                return tokens.get("api_key")
        return None

    async def is_configured(self) -> bool:
        """Check if a MiniMax API key is present."""
        key = await self._get_current_creds()
        return self._is_valid_credential(key)

    def _get_host(self) -> str:
        """Get host from config or default."""
        if settings.MINIMAX_HOST:
            return settings.MINIMAX_HOST
        return "minimax.io"

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect usage data from MiniMax via API."""
        self.api_key = await self._get_current_creds()
        self.host = self._get_host()

        if not self.api_key:
            return []

        return await self._fetch_via_api(client)

    async def _fetch_via_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fetch usage via the API endpoint.

        `/v1/coding_plan/remains` is the endpoint this collector has always used and
        it is confirmed working; MiniMax also documents a newer `/v1/token_plan/remains`
        path (same host) that we haven't migrated to.
        """
        base_url = f"https://api.{self.host}"

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            resp = await client.get(
                f"{base_url}/v1/coding_plan/remains",
                headers=headers,
                timeout=10,
            )

            if resp.status_code == 200:
                data = resp.json()
                return self._parse_api_response(data)

            logger.error(f"MiniMax API error (HTTP {resp.status_code}): {resp.text[:200]}")

        except Exception as e:
            logger.error(f"Failed to collect MiniMax usage: {e}")

        return []

    def _parse_api_response(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse `/v1/coding_plan/remains` and build one card per active window.

        Real response shape (nothing like the field names this parser used to
        assume — `model_remains[]` never had a `remains` field):

            {"model_remains": [{
                "model_name": "general",
                "start_time": <ms>, "end_time": <ms>, "remains_time": <ms>,
                "current_interval_total_count": 0, "current_interval_usage_count": 0,
                "current_interval_status": 1, "current_interval_remaining_percent": 100,
                "weekly_start_time": <ms>, "weekly_end_time": <ms>,
                "current_weekly_total_count": 0, "current_weekly_usage_count": 0,
                "current_weekly_status": 1, "current_weekly_remaining_percent": 100
            }, {"model_name": "video", ..., "current_interval_status": 3}],
             "base_resp": {"status_code": 0, "status_msg": "success"}}

        `model_remains` holds one entry per quota bucket, not per billable model —
        "general" covers the text/coding models, "video" is a separate entitlement
        this collector doesn't surface. Only entries with `current_interval_status
        == 1` (observed as "entitled"; MiniMax doesn't document the status enum) are
        considered. The weekly block is identical across every entry (one
        account-level weekly quota), so it's emitted once, from the first
        entitled entry, not per entry.
        """
        now_str = datetime.now(UTC).isoformat()

        model_remains = data.get("model_remains", [])
        entitled = [item for item in model_remains if item.get("current_interval_status") == 1]

        if not entitled:
            return [
                {
                    "service_name": "MiniMax",
                    "icon": "🤖",
                    "remaining": "No active plan",
                    "unit": "quota",
                    "reset": "—",
                    "health": "good",
                    "pace": "N/A",
                    "detail": "No active plan",
                    "data_source": self.DATA_SOURCE_API,
                    "input_source": getattr(self, "_current_input_source", "unknown"),
                    "is_unlimited": False,
                    "unit_type": "unknown",
                    "updated_at": now_str,
                }
            ]

        results = [self._build_session_card(item, now_str) for item in entitled]
        results.append(self._build_weekly_card(entitled[0], now_str))
        return results

    def _window_pct_used(
        self, total_count: int, usage_count: int, remaining_percent: float
    ) -> float:
        """`*_remaining_percent` is remaining, not used — invert it. Count fields win
        when the plan has started reporting them (`total_count > 0`); until then fall
        back to the percent field, which MiniMax appears to hold at 100 pre-use."""
        if total_count > 0:
            return (usage_count / total_count) * 100
        return 100.0 - remaining_percent

    def _build_session_card(self, item: dict[str, Any], now_str: str) -> dict[str, Any]:
        total_count = item.get("current_interval_total_count", 0)
        usage_count = item.get("current_interval_usage_count", 0)
        remaining_percent = item.get("current_interval_remaining_percent", 100.0)
        pct_used = self._window_pct_used(total_count, usage_count, remaining_percent)

        reset_at = None
        end_time = item.get("end_time")
        if end_time:
            reset_at = datetime.fromtimestamp(end_time / 1000, tz=UTC)

        return self._build_card(
            variant_label="5h",
            window_type="session",
            total_count=total_count,
            usage_count=usage_count,
            pct_used=pct_used,
            reset_at=reset_at,
            now_str=now_str,
        )

    def _build_weekly_card(self, item: dict[str, Any], now_str: str) -> dict[str, Any]:
        total_count = item.get("current_weekly_total_count", 0)
        usage_count = item.get("current_weekly_usage_count", 0)
        remaining_percent = item.get("current_weekly_remaining_percent", 100.0)
        pct_used = self._window_pct_used(total_count, usage_count, remaining_percent)

        reset_at = None
        weekly_end_time = item.get("weekly_end_time")
        if weekly_end_time:
            reset_at = datetime.fromtimestamp(weekly_end_time / 1000, tz=UTC)

        return self._build_card(
            variant_label="weekly",
            window_type="weekly",
            total_count=total_count,
            usage_count=usage_count,
            pct_used=pct_used,
            reset_at=reset_at,
            now_str=now_str,
        )

    def _build_card(
        self,
        *,
        variant_label: str,
        window_type: str,
        total_count: int,
        usage_count: int,
        pct_used: float,
        reset_at: datetime | None,
        now_str: str,
    ) -> dict[str, Any]:
        health = HealthCalculator.from_percentage(pct_used)
        pace = PaceCalculator.estimate_longevity(pct_used, reset_at)

        # Parenthesize the window label rather than trailing it after "· " — the
        # base collector's account-label auto-discovery (base.py's `user_match`
        # regex) treats a bare token after "·"/"|" at the end of `detail` as a
        # discovered account label, and would otherwise mislabel the account
        # "5h"/"weekly" instead of leaving it "Default".
        if total_count > 0:
            used_value = float(usage_count)
            limit_value = float(total_count)
            unit_type = "requests"
            allowance = f"{usage_count:,}/{total_count:,} prompts ({variant_label})"
        else:
            # No count data yet (fresh subscription, nothing consumed) — fall back
            # to the percent shape so the card still carries a real pct_used.
            used_value = pct_used
            limit_value = 100.0
            unit_type = "percent"
            allowance = f"—/— prompts ({variant_label})"

        return {
            "service_name": "MiniMax",
            "icon": "🤖",
            "remaining": f"{100 - pct_used:.1f}%",
            "unit": "capacity",
            "reset": human_delta(reset_at),
            "health": health,
            "pace": pace,
            "detail": f"{allowance} [API]",
            "used_value": used_value,
            "limit_value": limit_value,
            "is_unlimited": False,
            "unit_type": unit_type,
            "pct_used": pct_used,
            "window_type": window_type,
            "model_id": None,
            "reset_at": reset_at.isoformat() if reset_at else None,
            "data_source": self.DATA_SOURCE_API,
            "input_source": getattr(self, "_current_input_source", "unknown"),
            "usage_url": self.USAGE_URL,
            "updated_at": now_str,
        }

    def _fallback_strategies(self) -> list[Any]:
        """No fallback tier — the HTML coding-plan page is a client-rendered SPA and
        can't be scraped; the API endpoint is the only source."""
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return the ultimate error card(s) when all strategies fail."""
        from app.core.utils import error_card

        if not self.api_key:
            return [error_card("MiniMax", "🤖", "Missing API key", error_type="missing_config")]

        return [error_card("MiniMax", "🤖", "API connection failed", error_type="api_error")]
