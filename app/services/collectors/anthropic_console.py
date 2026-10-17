"""Claude Console balances. Console and Claude.ai sessions are independent."""

import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import httpx

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import HealthCalculator, error_card, http_request_with_retry
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

CONSOLE_URL = "https://platform.claude.com"


class ConsoleAPIError(Exception):
    """A sanitized upstream failure safe to display without leaking credentials."""

    def __init__(self, message: str, error_type: str, status_code: int = 502):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code
        self.retry_after = 300.0


def console_headers(cookie: str) -> dict[str, str]:
    value = cookie.strip()
    if not value or "\r" in value or "\n" in value:
        raise ConsoleAPIError("Enter a Console session cookie", "missing_config", 422)
    return {
        "Cookie": value if "=" in value else f"sessionKey={value}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": CONSOLE_URL,
        "Referer": f"{CONSOLE_URL}/settings/billing",
    }


async def console_get(client: httpx.AsyncClient, path: str, cookie: str) -> Any:
    try:
        response = await http_request_with_retry(
            client,
            "GET",
            CONSOLE_URL + path,
            headers=console_headers(cookie),
            timeout=10.0,
            retry_on_429=False,
        )
    except httpx.HTTPError as exc:
        raise ConsoleAPIError("Console connection failed", "timeout") from exc
    if response.status_code in (401, 403):
        raise ConsoleAPIError(
            "Console session expired or billing access denied. Replace the Console cookie.",
            "auth_failed",
            403,
        )
    if response.status_code == 429:
        error = ConsoleAPIError("Console rate limited; retrying later", "rate_limited", 429)
        try:
            wait = float(response.headers.get("Retry-After", "300"))
            if math.isfinite(wait):
                error.retry_after = max(300.0, wait)
        except ValueError:
            pass
        raise error
    if response.status_code != 200:
        raise ConsoleAPIError("Console billing request failed", "api_error")
    try:
        return response.json()
    except ValueError as exc:
        raise ConsoleAPIError("Invalid Console response", "parse_error") from exc


async def console_organizations(client: httpx.AsyncClient, cookie: str) -> list[dict[str, str]]:
    data = await console_get(client, "/api/organizations", cookie)
    if not isinstance(data, list):
        raise ConsoleAPIError("Invalid Console organization response", "parse_error")
    result = []
    for org in data:
        if not isinstance(org, dict) or "api" not in (org.get("capabilities") or []):
            continue
        try:
            org_id = str(UUID(org.get("uuid") or org.get("id")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ConsoleAPIError("Invalid Console organization identity", "parse_error") from exc
        result.append({"id": org_id, "name": str(org.get("name") or "API organization")})
    return result


def _amount(value: Any, exponent: Any = 2) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError("Invalid credit amount")
    if isinstance(exponent, bool) or not isinstance(exponent, int) or not 0 <= exponent <= 6:
        raise ValueError("Invalid credit exponent")
    try:
        amount = Decimal(str(value)) / (Decimal(10) ** exponent)
    except InvalidOperation as exc:
        raise ValueError("Invalid credit amount") from exc
    result = float(amount)
    if not math.isfinite(result):
        raise ValueError("Invalid credit amount")
    return result


def _money(data: dict[str, Any], nested: str, legacy: str) -> float:
    value = data.get(nested)
    if isinstance(value, dict):
        for kind in ("money", "credits"):
            part = value.get(kind)
            if isinstance(part, dict):
                return _amount(part.get("amount_minor"), part.get("exponent", 2))
    return _amount(data.get(legacy))


def parse_console_balance(data: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Keep the upstream total authoritative; tranches explain, never increase it."""
    if not isinstance(data, dict):
        raise ValueError("Invalid Console balance")
    now = now or datetime.now(UTC)
    currency = data.get("currency")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise ValueError("Missing Console currency")
    currency = currency.upper()
    total = _money(data, "balance", "amount")
    breakdown: list[dict[str, Any]] = []
    for key, default_kind in (("tranches", "purchased"), ("promo_tranches", "promotion")):
        tranches = data.get(key, [])
        if not isinstance(tranches, list):
            raise ValueError("Invalid Console credit grants")
        for tranche in tranches:
            if not isinstance(tranche, dict):
                raise ValueError("Invalid Console credit grant")
            expires = tranche.get("expires_at")
            if expires:
                expiry = parse_iso8601_utc(expires)
                if expiry is None:
                    raise ValueError("Invalid credit expiry")
                if expiry <= now:
                    continue
            remaining = _money(tranche, "remaining", "remaining_amount_minor_units")
            granted = _money(tranche, "granted", "granted_amount_minor_units")
            if remaining < 0 or granted < remaining:
                raise ValueError("Invalid credit grant amounts")
            kind = (
                "subscription"
                if tranche.get("program_id") == "subscription_api_credit"
                else default_kind
            )
            scope = tranche.get("scope") or {}
            products = sorted(
                {
                    item["product"]
                    for item in scope.get("applies_to", [])
                    if isinstance(item, dict) and isinstance(item.get("product"), str)
                }
            )
            breakdown.append(
                {
                    "kind": kind,
                    "label": {
                        "subscription": "Subscription API credits",
                        "purchased": "Purchased credits",
                    }.get(kind, str(tranche.get("name") or "Promotional credits")),
                    "currency": str(tranche.get("currency") or currency).upper(),
                    "remaining_value": remaining,
                    "granted_value": granted,
                    "expires_at": expires,
                    "products": products,
                }
            )
    symbol = "$" if currency == "USD" else f"{currency} "
    return {
        "service_name": "Claude Console",
        "variant": "Balance",
        "window_type": "rolling",
        "icon": "🟠",
        "remaining": f"{symbol}{total:.2f}",
        "unit": "remaining",
        "unit_type": "currency",
        "currency": currency,
        "reset": "Prepaid",
        "health": HealthCalculator.from_balance(total),
        "pace": "Prepaid balance",
        "detail": "Console balance including purchased and promotional credits",
        "credit_breakdown": breakdown,
        "data_source": "web",
        "usage_url": f"{CONSOLE_URL}/settings/billing",
        "updated_at": now.isoformat(),
    }


class AnthropicConsoleCollector(BaseCollector):
    PROVIDER_ID = "anthropic_console"
    DEFAULT_WINDOW_TYPE = "rolling"
    COMPLETE_SNAPSHOT = True
    CREDENTIALS_KEYED_BY_ACCOUNT_ID = True
    STRATEGIES = {"web": ("Console billing (web)", "_primary_strategy")}

    def _cookie(self) -> str | None:
        account_id = getattr(self, "credential_account_id", None) or self.account_id or "default"
        metadata = token_cache.current_source_metadata(self.PROVIDER_ID, account_id) or {}
        self._current_input_source = (
            "sidecar" if metadata.get("source_type") in ("cookie", "keychain") else "config"
        )
        return credential_provider.get_provider_session_cookie(
            self.PROVIDER_ID, account_id=account_id
        )

    async def is_configured(self) -> bool:
        return bool(self._cookie())

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        cookie = self._cookie()
        if not cookie:
            return []
        try:
            org_id = str(UUID(self.account_id or ""))
            data = await console_get(client, f"/api/organizations/{org_id}/prepaid/credits", cookie)
            return [parse_console_balance(data)]
        except ConsoleAPIError as exc:
            self._last_retry_after = exc.retry_after
            return [error_card("Claude Console", "🟠", str(exc), error_type=exc.error_type)]
        except (ValueError, TypeError, AttributeError):
            return [
                error_card(
                    "Claude Console",
                    "🟠",
                    "Invalid Console balance response",
                    error_type="parse_error",
                )
            ]

    async def _fallback_strategies(self) -> list[Any]:
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        return [
            error_card(
                "Claude Console",
                "🟠",
                "Configure a Console session cookie",
                error_type="missing_config",
            )
        ]
