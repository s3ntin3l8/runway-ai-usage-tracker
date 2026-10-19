"""Read-only verification of a single pinned native Claude OAuth credential."""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

import httpx

from app.core.utils import error_card
from app.services.account_identity import canonical_account_id
from app.services.collectors._anthropic_common import anthropic_limits_from
from app.services.token_cache import token_cache


def _valid_quota(usage: dict[str, Any]) -> bool:
    def finite(value: Any) -> bool:
        return (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )

    limits = anthropic_limits_from(usage)
    if limits is not None:
        return all(
            isinstance(entry, dict)
            and finite(entry.get("percent"))
            and isinstance(entry.get("group"), str)
            and bool(entry["group"])
            and (entry.get("scope") is None or isinstance(entry["scope"], dict))
            for entry in limits
        )
    windows = [
        value
        for name, value in usage.items()
        if name == "five_hour" or name.startswith("seven_day")
    ]
    present = [value for value in windows if value is not None]
    return bool(present) and all(
        isinstance(value, dict) and finite(value.get("utilization")) for value in present
    )


async def collect_native_oauth(collector: Any, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Never refresh; both profile and quota must succeed for this exact generation."""
    tokens = dict(
        token_cache.current_source_tokens("anthropic", collector.account_id or "default") or {}
    )
    token = tokens.get("oauth_token")

    def failure(message: str, kind: str) -> list[dict[str, Any]]:
        return [error_card("Claude", "🟠", message, error_type=kind)]

    if not token:
        return []
    if await collector._is_token_expired():
        return failure("Native login expired; renew in the owning app", "auth_failed")
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.271",
    }

    def response_failure(response: httpx.Response) -> list[dict[str, Any]]:
        if response.status_code == 429:
            delay = response.headers.get("Retry-After", "300")
            collector._last_retry_after = max(300, int(delay) if delay.isdigit() else 300)
            return failure("Native OAuth rate limited", "rate_limited")
        if response.status_code in (401, 403):
            return failure("Native OAuth access rejected", "auth_failed")
        return failure(f"Native OAuth API HTTP {response.status_code}", "api_error")

    try:
        profile_response = await client.get(
            "https://api.anthropic.com/api/oauth/profile", headers=headers, timeout=10
        )
        if profile_response.status_code != 200:
            return response_failure(profile_response)
        profile = profile_response.json()
        account = profile.get("account") if isinstance(profile, dict) else None
        if not isinstance(account, dict):
            return failure("Native OAuth holder unavailable", "parse_error")
        email = account.get("email")
        from app.services.account_identity import EMAIL_RE

        if not isinstance(email, str) or not EMAIL_RE.fullmatch(email):
            return failure("Native OAuth holder unavailable", "parse_error")
        if tokens.get("oauth_account_uuid"):
            try:
                bound = UUID(tokens["oauth_account_uuid"])
                holder = UUID(account.get("uuid", ""))
            except (ValueError, TypeError, AttributeError):
                return failure("Native OAuth account binding unavailable", "auth_failed")
            if holder != bound:
                return failure("Native OAuth account binding mismatch", "auth_failed")
        identity = canonical_account_id(email)
        if (
            collector.account_id not in (None, "default")
            and canonical_account_id(collector.account_id) != identity
        ):
            return failure("Native OAuth belongs to another account", "auth_failed")
        usage_response = await client.get(
            "https://api.anthropic.com/api/oauth/usage", headers=headers, timeout=10
        )
        if usage_response.status_code != 200:
            return response_failure(usage_response)
        usage = usage_response.json()
        if not isinstance(usage, dict):
            return failure("Native OAuth quota unavailable", "parse_error")
        if not _valid_quota(usage):
            return failure("Native OAuth quota unavailable", "parse_error")
    except httpx.HTTPError:
        return failure("Native OAuth request unavailable", "timeout")
    except (ValueError, TypeError):
        return failure("Unsupported native OAuth response", "parse_error")
    if token_cache.current_source_tokens("anthropic", collector.account_id or "default") != tokens:
        return failure("Native OAuth credential changed during verification", "unavailable")
    collector._current_input_source = "sidecar"
    try:
        cards = collector._parse_oauth_response(usage, collector._name_map, {"email": identity})
    except (ValueError, TypeError, AttributeError, KeyError):
        return failure("Unsupported native OAuth quota", "parse_error")
    if not cards or any(
        value is not None
        and (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
        )
        for card in cards
        for value in (card.get("used_value"), card.get("limit_value"))
    ):
        return failure("Unsupported native OAuth numeric quota", "parse_error")
    collector.verified_source_generation = token_cache.credential_generation(tokens)
    collector.verified_identity = identity
    collector.verified_subject = account.get("uuid")
    collector.verified_organization_id = None
    collector.account_id = collector.account_label = identity
    return cards
