"""What a Claude OAuth token's account publishes, and how it bills.

The OAuth strategy (``app/services/collectors/anthropic_oauth.py``) and the
manual pricing path (the preview endpoint) must agree on how an OAuth account
bills, so the reading of the usage payload lives here, once: the strategy
publishes these cards, and the endpoint prices the same credential from the
same reading.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import (
    HealthCalculator,
    PaceCalculator,
    error_card,
    http_request_with_retry,
    human_delta,
)
from app.services.billing_type import UNKNOWN, BillingType, detect_billing_type
from app.services.collectors._anthropic_common import (
    ANTHROPIC_WINDOW_NAME_MAP,
    anthropic_limits_from,
    anthropic_model_id_for,
    anthropic_published_number,
    anthropic_published_percent,
    build_anthropic_limit_cards,
    classify_anthropic_window_type,
)

logger = logging.getLogger(__name__)

#: Where an OAuth token reports what it may still use.
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

#: The keys the usage payload spends its balance under.
BALANCE_KEYS = ("current_balance", "available_balance", "balance", "credits")

#: Where the cards read on the manual path came from. The detector reads the
#: card's shape, not where it came from, but a card still says where it was
#: read, and these are the same words the strategy's own cards carry.
OAUTH_DATA_SOURCE = "api"
OAUTH_INPUT_SOURCE = "config"


class OAuthUsageError(Exception):
    """The OAuth usage endpoint could not be read for this token.

    ``message`` is what the operator should read; ``error_type`` says what
    kind of failure it was, the way the collector's error cards say it.
    """

    def __init__(
        self,
        message: str,
        error_type: str,
        status_code: int = 0,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.status_code = status_code
        self.retry_after = retry_after


async def fetch_oauth_usage(client: httpx.AsyncClient, token: str) -> dict[str, Any]:
    """The usage payload an OAuth token publishes.

    Raises ``OAuthUsageError`` when the endpoint cannot be read for this
    token — an expired one, a rate limit, a transport failure, a response
    that is not the payload we asked for.
    """
    if token.startswith("sk-ant-sid") or "sessionKey=" in token:
        # A browser session key, not an OAuth token: the web strategy owns it.
        raise OAuthUsageError("Not an OAuth token", "unsupported_credential")

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "claude-code/2.1.69",
        "anthropic-beta": "oauth-2025-04-20",
    }
    try:
        resp = await http_request_with_retry(
            client, "GET", OAUTH_USAGE_URL, headers=headers, timeout=10.0
        )
    except httpx.HTTPError as exc:
        raise OAuthUsageError(f"Conn Fail: {str(exc)[:20]}", "timeout") from exc

    if resp.status_code == 401:
        raise OAuthUsageError("Expired/Invalid Token (OAuth)", "auth_failed", 401)
    if resp.status_code == 429:
        # A usage-endpoint 429 is a rate limit, not an expired token. Refresh
        # tokens are single-use (a refresh rotates them), so never spend one
        # as a remedy: back off and let the token refresh near its expiry.
        retry_after = resp.headers.get("Retry-After")
        wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
        wait_sec = max(wait_sec, 300)
        raise OAuthUsageError(
            f"Rate Limited (429) - Try in {wait_sec / 60:.1f}m",
            "rate_limited",
            429,
            wait_sec,
        )
    if resp.status_code != 200:
        raise OAuthUsageError(f"API Error {resp.status_code}", "api_error", resp.status_code)
    try:
        return resp.json()
    except (ValueError, KeyError) as exc:
        raise OAuthUsageError("Invalid API response", "api_error") from exc


def oauth_identity_string(data: Mapping[str, Any] | None) -> str:
    """The identity an OAuth payload or its organization info describes."""
    if not data:
        return ""
    email = data.get("email")
    org = data.get("organization")
    if email and org:
        return f"{email} @ {org}"
    if email:
        return email
    if org:
        return f"org: {org}"
    account = data.get("account", {})
    email = account.get("email", "")
    org = account.get("organization", "")
    if email and org:
        return f"{email} @ {org}"
    if email:
        return email
    if org:
        return f"org: {org}"
    return ""


def oauth_organization_name(
    data: Mapping[str, Any], api_account_info: Mapping[str, Any] | None = None
) -> str:
    """The account's name: the organization names it.

    Its contact address is not who holds the token, so it never becomes the
    account's name.
    """
    return str((api_account_info or {}).get("organization") or "") or str(
        (data.get("account") or {}).get("organization") or ""
    )


def oauth_usage_cards(  # noqa: PLR0915 — known-debt: provider response shape, splits poorly
    data: dict[str, Any],
    name_map: dict[str, str],
    api_account_info: dict | None = None,
    *,
    data_source: str,
    input_source: str,
) -> list[dict[str, Any]]:
    """The cards an OAuth usage payload describes."""
    results = []

    # Infer plan/tier from the organization info, then from the usage response.
    tier = api_account_info.get("tier") if api_account_info else None

    # Final fallback from data (if API ever includes it)
    if not tier:
        account = data.get("account", {})
        plan = account.get("plan", "")
        tier = plan.capitalize() if plan else None

    # Resolve identity
    identity_str = ""
    if api_account_info:
        identity_str = oauth_identity_string(api_account_info)

    if not identity_str:
        identity_str = oauth_identity_string(data)

    identity_suffix = f" | {identity_str}" if identity_str else ""

    # The account's *name*: the organization names it. Its contact address
    # is not who holds the token, so it never becomes the account's name —
    # and a name the operator chose is never overwritten.
    name_str = oauth_organization_name(data, api_account_info)

    # Newer OAuth response shape: a top-level `limits` array of self-describing
    # window objects (kind/group/percent/severity/scope) that coexists with (and
    # duplicates) the legacy dict-keyed-by-window-name shape. Parse it directly
    # as authoritative and skip synthesizing empty core cards for those, since the
    # array already supplies session/weekly (and any per-model weekly).
    # A present-but-empty array (partial rollout) falls back to the legacy loop.
    limits = anthropic_limits_from(data)
    core_keys: list[str]
    if limits is not None:
        results.extend(
            build_anthropic_limit_cards(
                limits,
                tier=tier,
                account_label=name_str,
                identity_suffix=identity_suffix,
                data_source=data_source,
                input_source=input_source,
                source_label="OAuth",
                usage_url="https://claude.ai/settings/usage",
            )
        )
        core_keys = []
    else:
        # Guaranteed keys to show even if null from API
        core_keys = [
            "five_hour",
            "seven_day",
        ]
    all_keys = list(data.keys())
    for ck in core_keys:
        if ck not in all_keys:
            all_keys.append(ck)

    def sort_key(k):
        try:
            return list(name_map.keys()).index(k)
        except ValueError:
            return 999

    for key in sorted(all_keys, key=sort_key):
        if key == "account":
            continue

        usage: Any = data.get(key)
        placeholder = False
        if usage is None:
            if key in core_keys:
                # The account published no such window: the collector
                # guarantees one, so the card it gets is a placeholder.
                usage = {"utilization": 0.0, "resets_at": None}
                placeholder = True
            else:
                continue

        u_type = name_map.get(key, key.replace("_", " ").title())

        # 1. Handle Balance/Currency fields (Prepaid or Specific Balance)
        if key in BALANCE_KEYS:
            try:
                bal = anthropic_published_number(usage, key) if usage is not None else 0.0
                results.append(
                    {
                        "service_name": "Claude",
                        "variant": u_type,
                        "icon": "💰",
                        "remaining": f"${bal:.2f}",
                        "unit": "USD",
                        "reset": "Prepaid",
                        "health": HealthCalculator.from_balance(bal),
                        "pace": "Manual Top-up",
                        "detail": f"Current Balance: ${bal:.2f} [OAuth]{identity_suffix}",
                        "used_value": 0.0,
                        "limit_value": bal,
                        "unit_type": "currency",
                        "window_type": "rolling",
                        "model_id": None,
                        # A claude.ai balance says how much credit is left, not
                        # how the account bills: a Claude plan can carry
                        # prepaid credit, so this card decides no model.
                        "billing_evidence": "prepaid_credit",
                        "data_source": data_source,
                        "input_source": input_source,
                        "tier": tier,
                        "account_label": name_str,
                        "usage_url": "https://claude.ai/settings/usage",
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )
                # Only emit one balance card even if multiple keys exist
                break
            except (ValueError, TypeError):
                logger.debug("Failed to parse balance tier", exc_info=True)
            continue

        # Skip overage when extra_usage is also present to avoid duplicate
        # spend/limit cards (Web API parser uses the same merge logic).
        if key == "overage" and data.get("extra_usage") is not None:
            continue

        if not isinstance(usage, dict):
            continue

        # 2. Handle Spend/Limit windows (Overage/Spent)
        raw_spend = usage.get("spend")
        raw_limit = usage.get("limit")
        limit = anthropic_published_number(raw_limit, "limit") if raw_limit is not None else 0.0
        if limit > 0:
            spend = anthropic_published_number(raw_spend, "spend") if raw_spend is not None else 0.0
            remaining = max(0.0, limit - spend)
            results.append(
                {
                    "service_name": "Claude",
                    "variant": u_type,
                    "icon": "💰",
                    "remaining": f"${remaining:.2f}",
                    "unit": "limit",
                    "reset": "Monthly",
                    "health": HealthCalculator.from_spend(spend, limit),
                    "pace": "Flexible",
                    "detail": f"Spent: ${spend:.2f} / ${limit:.2f} [OAuth]{identity_suffix}",
                    "used_value": spend,
                    "limit_value": limit,
                    "unit_type": "currency",
                    "window_type": "monthly",
                    "model_id": None,
                    "data_source": data_source,
                    "input_source": input_source,
                    "tier": tier,
                    "account_label": name_str,
                    "usage_url": "https://claude.ai/settings/usage",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            continue

        # 3. Handle Standard Percentage windows
        # limits[] is authoritative for percentage windows when present — skip
        # legacy keys here to avoid duplicate/phantom cards (e.g. five_hour vs
        # the "session" entry, or a disabled "spend"/"extra_usage" object that
        # has neither a matching balance nor spend/limit shape).
        if limits is not None:
            continue

        raw_utilization = anthropic_published_percent(usage, "utilization", "percentUsed")
        if raw_utilization is None:
            # The account published the window but no usable number in it:
            # the card the collector draws is a placeholder, not evidence.
            placeholder = True
        pct_used = 0.0 if raw_utilization is None else raw_utilization
        remaining_pct = 100.0 - pct_used

        reset_raw = usage.get("resets_at") or usage.get("resetsAt")
        reset_at = None
        if reset_raw:
            try:
                reset_at = parse_iso8601_utc(reset_raw)
            except (ValueError, TypeError):
                logger.debug("Failed to parse reset_at in OAuth window", exc_info=True)

        w_type = classify_anthropic_window_type(key)
        service_name = "Claude"

        card: dict[str, Any] = {
            "service_name": service_name,
            "icon": "🟠",
            "remaining": f"{remaining_pct:.1f}%",
            "unit": "capacity",
            "reset": human_delta(reset_at),
            "health": HealthCalculator.from_percentage(pct_used),
            "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
            "detail": f"{pct_used:.1f}% used [OAuth]{identity_suffix}",
            "used_value": pct_used,
            "limit_value": 100.0,
            "is_unlimited": False,
            "unit_type": "percent",
            "window_type": w_type,
            "model_id": anthropic_model_id_for(key),
            "reset_at": reset_at.isoformat() if reset_at else None,
            "data_source": data_source,
            "input_source": input_source,
            "tier": tier,
            "account_label": name_str,
            "usage_url": "https://claude.ai/settings/usage",
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if placeholder:
            # The account never published this window, so the card is the
            # collector's placeholder, not the account's evidence.
            card["billing_evidence"] = "placeholder"
        results.append(card)

    return (
        results
        if results
        else [error_card("Claude Pro", "🟠", "No quota data", error_type="parse_error")]
    )


def billing_model_from_oauth_cards(cards: Iterable[Mapping[str, Any]]) -> BillingType | None:
    """The model the OAuth cards describe, or ``None`` when they describe none.

    ``None`` is the absence of a proof, not a proof of "unknown": the
    operator (or the account's own later proof) decides, and an inconclusive
    answer is never stored as a decision.
    """
    model = detect_billing_type(cards)
    return None if model == UNKNOWN else model


async def billing_model_from_oauth_usage(
    client: httpx.AsyncClient, token: str
) -> BillingType | None:
    """The model an OAuth token's own usage describes, or ``None``.

    An OAuth token has no Claude organization to read, so the usage its own
    strategy publishes is the only evidence there is — and it is read exactly
    the way the strategy reads it, so both paths reach the same answer for the
    same credential.

    A usage payload that cannot be read (an expired token, a rate limit, a
    transport failure) proves nothing: the answer is the absence of a proof,
    never a decision.

    A payload that cannot be *understood* proves nothing either. The preview
    this serves is an answer to a question, not a verdict on the account, so a
    quota the collector cannot read ends the same way: no proof, no decision.
    """
    try:
        data = await fetch_oauth_usage(client, token)
    except OAuthUsageError as exc:
        logger.info("OAuth usage could not be read for the billing model: %s", exc.message)
        return None
    try:
        cards = oauth_usage_cards(
            data,
            ANTHROPIC_WINDOW_NAME_MAP,
            None,
            data_source=OAUTH_DATA_SOURCE,
            input_source=OAUTH_INPUT_SOURCE,
        )
    except Exception:
        # A corrupt quota is an error, not a missing one, and this path never
        # raises: the operator asked what the account bills, and the honest
        # answer to "the usage payload is unreadable" is no answer at all.
        # The traceback is logged, so a fault in the collector is visible here
        # rather than looking like an account that publishes no quota.
        logger.warning("OAuth usage could not be understood for the billing model", exc_info=True)
        return None
    return billing_model_from_oauth_cards(cards)
