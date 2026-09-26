import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import HealthCalculator, http_request_with_retry
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class DeepSeekCollector(BaseCollector):
    """
    Collector for DeepSeek prepaid balance (pay-as-you-go API account).
    Uses: https://api.deepseek.com/user/balance  (account-level)

    DeepSeek exposes no per-window quota via API — the only upstream gauge is
    the topped-up/granted balance. Token usage reaches Runway through the
    sidecar's opencode event extractor (BYOK ``providerID="deepseek"``).
    """

    PROVIDER_ID = "deepseek"
    DEFAULT_WINDOW_TYPE = "rolling"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)

    async def _get_api_key(self) -> str | None:
        """Discover API key: DB (this account, then default) → token cache → env.

        The dashboard's multi-account wizard stores a pasted ``sk-`` key under
        a hashed ``credential_hint`` account_id, which the legacy unscoped read
        deliberately hides (multi-account safety) — so scope the DB read by
        this collector's account first and only then fall back to the default
        row.
        """
        for acc in dict.fromkeys([self.account_id, "default"]):
            if not acc:
                continue
            db_key = credential_provider.get_provider_api_key("deepseek", account_id=acc)
            if db_key:
                self._current_input_source = "config"
                return db_key

        if self.account_id:
            cache_data = await token_cache.get_with_metadata("deepseek", account_id=self.account_id)
            if cache_data:
                tokens, metadata = cache_data
                # The DB→cache mirror writes every key into ``oauth_token``;
                # only some collectors read the ``api_key`` slot. A cache row
                # without a usable key field is NOT terminal — fall through so
                # the env default below still gets its chance.
                cached_key = tokens.get("api_key") or tokens.get("oauth_token")
                if cached_key:
                    source = metadata.get("source") or "sidecar"
                    self._current_input_source = (
                        "config" if source in ("config", "manual_config") else "sidecar"
                    )
                    return cached_key

        if self.account_id not in (None, "default"):
            # The env var belongs to the default account; a dynamic collector
            # for another account must not claim it (duplicate card risk).
            return None

        key = settings.DEEPSEEK_API_KEY or None
        if key:
            self._current_input_source = "server"
        return key

    async def is_configured(self) -> bool:
        """Check if a DeepSeek API key is present."""
        return self._is_valid_credential(await self._get_api_key())

    @staticmethod
    def _pick_balance(balance_infos: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Prefer the USD entry when multiple currencies are returned."""
        parsed: list[dict[str, Any]] = []
        for info in balance_infos:
            try:
                parsed.append(
                    {
                        "currency": str(info.get("currency") or "USD").upper(),
                        "total": float(info.get("total_balance") or 0.0),
                        "granted": float(info.get("granted_balance") or 0.0),
                        "topped_up": float(info.get("topped_up_balance") or 0.0),
                    }
                )
            except (TypeError, ValueError):
                continue
        if not parsed:
            return None
        for info in parsed:
            if info["currency"] == "USD":
                return info
        return parsed[0]

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect the prepaid balance from the DeepSeek balance API."""
        api_key = await self._get_api_key()
        if not api_key:
            return []

        self._api_key = api_key
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

        try:
            resp = await http_request_with_retry(
                client, "GET", f"{DEEPSEEK_BASE_URL}/user/balance", headers=headers, timeout=10
            )
        except Exception as e:
            logger.error(f"Failed to collect DeepSeek balance: {e}")
            return []

        if resp.status_code != 200:
            logger.error(f"DeepSeek balance API error (HTTP {resp.status_code}): {resp.text}")
            return []

        try:
            data = resp.json()
        except Exception as e:
            logger.error(f"DeepSeek balance API returned non-JSON body: {e}")
            return []

        infos = data.get("balance_infos")
        if not isinstance(infos, list) or not infos:
            logger.error("DeepSeek balance API response missing balance_infos")
            return []

        balance = self._pick_balance([i for i in infos if isinstance(i, dict)])
        if balance is None:
            logger.error("DeepSeek balance API returned no parseable balance entries")
            return []

        total = balance["total"]
        granted = balance["granted"]
        topped_up = balance["topped_up"]
        available = bool(data.get("is_available", True))
        currency = balance["currency"]
        symbol = "$" if currency == "USD" else f"{currency} "

        detail = f"Paid: {symbol}{topped_up:.2f} / Granted: {symbol}{granted:.2f} [API]"
        if not available:
            detail += " — balance unavailable for API calls"

        health = HealthCalculator.from_balance(total)
        if not available and health == "good":
            health = "warning"

        return [
            {
                "service_name": "DeepSeek",
                "variant": "Balance",
                "window_type": "rolling",
                "icon": "🐋",
                "remaining": f"{symbol}{total:.2f}",
                "unit": currency,
                "reset": "Prepaid",
                "health": health,
                "pace": "Stable",
                "detail": detail,
                "unit_type": "currency",
                "currency": currency,
                "data_source": self.DATA_SOURCE_API,
                "usage_url": "https://platform.deepseek.com/usage",
                "input_source": getattr(self, "_current_input_source", "unknown"),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ]

    def _fallback_strategies(self) -> list[Any]:
        """Return an ordered list of fallback async methods. Currently none."""
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return the ultimate error card(s) when all strategies fail."""
        from app.core.utils import error_card

        api_key = await self._get_api_key()
        if not api_key:
            return [
                error_card(
                    "DeepSeek", "🐋", "Missing DEEPSEEK_API_KEY", error_type="missing_config"
                )
            ]

        return [error_card("DeepSeek", "🐋", "API connection failed", error_type="api_error")]
