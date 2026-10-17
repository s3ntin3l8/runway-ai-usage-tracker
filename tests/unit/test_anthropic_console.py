"""Console billing is a separate session and balance, never a quota fallback."""

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest

from app.models.schemas import LimitCard
from app.services.collectors.anthropic_console import (
    AnthropicConsoleCollector,
    ConsoleAPIError,
    console_headers,
    console_organizations,
    parse_console_balance,
)
from app.services.smart_collector import SmartCollector

ORG_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2030, 1, 1, tzinfo=UTC)
BALANCE = {
    "amount": 14000,
    "currency": "USD",
    "balance": {"money": None, "credits": {"amount_minor": 14000, "exponent": 2}},
    "tranches": [{"granted_amount_minor_units": 5000, "remaining_amount_minor_units": 4000}],
    "promo_tranches": [
        {
            "program_id": "subscription_api_credit",
            "currency": "USD",
            "granted_amount_minor_units": 10000,
            "remaining_amount_minor_units": 10000,
            "granted_at": "2029-12-13T00:00:00Z",
            "expires_at": "2030-01-16T00:00:00Z",
            "scope": {
                "kind": "scoped",
                "applies_to": [{"product": "api"}, {"product": "agent_sdk"}],
            },
        }
    ],
}


def test_total_is_not_sum_of_balance_and_grants():
    card = LimitCard(**parse_console_balance(BALANCE, now=NOW))
    assert card.remaining == "$140.00"
    assert card.used_value is None and card.limit_value is None and card.pct_used is None
    assert card.reset_at is None
    assert card.data_source == "web"
    assert card.credit_breakdown is not None
    grant = card.credit_breakdown[1]
    assert grant.kind == "subscription"
    assert grant.remaining_value == 100
    assert grant.granted_value == 100
    assert grant.products == ["agent_sdk", "api"]
    assert grant.expires_at == "2030-01-16T00:00:00Z"


@pytest.mark.parametrize("remaining", [0, 2500, 10000])
def test_subscription_used_is_exact_difference(remaining):
    data = deepcopy(BALANCE)
    data["promo_tranches"][0]["remaining_amount_minor_units"] = remaining
    card = parse_console_balance(data, now=NOW)
    grant = card["credit_breakdown"][1]
    assert grant["granted_value"] - grant["remaining_value"] == (10000 - remaining) / 100


def test_expired_grants_omitted_without_changing_reported_total():
    card = parse_console_balance(BALANCE, now=datetime(2031, 1, 1, tzinfo=UTC))
    assert card["remaining"] == "$140.00"
    assert len(card["credit_breakdown"]) == 1


def test_nested_money_exponent_currency_and_other_promotions():
    data = deepcopy(BALANCE)
    data["currency"] = "JPY"
    data["balance"] = {"money": {"amount_minor": 1234, "exponent": 0}}
    data["promo_tranches"][0].update(
        {
            "program_id": "other",
            "name": "Trial",
            "currency": "EUR",
            "remaining": {"money": {"amount_minor": 3000, "exponent": 3}},
            "granted": {"credits": {"amount_minor": 5000, "exponent": 3}},
        }
    )
    card = parse_console_balance(data, now=NOW)
    assert card["remaining"] == "JPY 1234.00"
    grant = card["credit_breakdown"][1]
    assert grant["kind"] == "promotion" and grant["label"] == "Trial"
    assert grant["remaining_value"] == 3 and grant["granted_value"] == 5
    assert grant["currency"] == "EUR"


def test_zero_balance_without_grants_is_valid():
    card = parse_console_balance(
        {"amount": 0, "currency": "USD", "tranches": [], "promo_tranches": []}
    )
    assert card["remaining"] == "$0.00" and card["credit_breakdown"] == []


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"amount": "NaN", "currency": "USD"},
        {"amount": True, "currency": "USD"},
        {"amount": 0, "currency": "USD", "tranches": None},
        {"amount": 0, "currency": "USD", "promo_tranches": [None]},
        {"currency": "USD", "balance": {"credits": {"amount_minor": 2, "exponent": -1}}},
    ],
)
def test_invalid_total_does_not_become_zero(data):
    with pytest.raises(ValueError):
        parse_console_balance(data)


@pytest.mark.parametrize(
    "change",
    [
        {"remaining_amount_minor_units": -1},
        {"remaining_amount_minor_units": 20000},
        {"expires_at": "garbage"},
        {"remaining_amount_minor_units": "Infinity"},
    ],
)
def test_malformed_grant_rejected(change):
    data = deepcopy(BALANCE)
    data["promo_tranches"][0].update(change)
    with pytest.raises(ValueError):
        parse_console_balance(data, now=NOW)


def test_cookie_header_accepts_bare_or_bundle_and_rejects_newlines():
    assert console_headers("sample-session")["Cookie"] == "sessionKey=sample-session"
    assert console_headers("sessionKey=sample-session; extra=value")["Cookie"].endswith(
        "extra=value"
    )
    with pytest.raises(ConsoleAPIError):
        console_headers("value\r\nX-Test: bad")


async def test_discovery_uses_api_capability_and_stable_uuid():
    def respond(request):
        assert request.url.host == "platform.claude.com"
        assert request.headers["cookie"] == "sessionKey=sample-session"
        return httpx.Response(
            200,
            json=[
                {"uuid": ORG_ID, "name": "API org", "capabilities": ["api"]},
                {"uuid": "not-an-api-org", "capabilities": ["chat"]},
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await console_organizations(client, "sample-session") == [
            {"id": ORG_ID, "name": "API org"}
        ]


@pytest.mark.parametrize(
    "status,error_type",
    [(401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited"), (500, "api_error")],
)
async def test_collector_errors_are_sanitized_and_independent(status, error_type):
    collector = AnthropicConsoleCollector(account_id=ORG_ID)
    with patch.object(collector, "_cookie", return_value="sample-session"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    status, json={"error": "sample-session"}, headers={"Retry-After": "1234"}
                )
            )
        ) as client:
            result = await collector.collect(client)
    assert result[0]["provider_id"] == "anthropic_console"
    assert result[0]["error_type"] == error_type
    assert "sample-session" not in str(result)
    if status == 429:
        assert collector._last_retry_after == 1234


async def test_collector_uses_pinned_org_and_returns_zero_not_empty():
    collector = AnthropicConsoleCollector(account_id=ORG_ID, account_label="API org")

    def respond(request):
        assert request.url.path == f"/api/organizations/{ORG_ID}/prepaid/credits"
        return httpx.Response(200, json={"amount": 0, "currency": "USD"})

    with patch.object(collector, "_cookie", return_value="sample-session"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            cards = await collector.collect(client)
    assert cards[0]["remaining"] == "$0.00"
    assert cards[0]["account_id"] == ORG_ID
    assert cards[0]["account_label"] == "API org"
    assert collector.complete_snapshot(cards)


async def test_failed_console_poll_retains_last_good_balance():
    collector = AnthropicConsoleCollector(account_id=ORG_ID)
    smart = SmartCollector(collector, "Console", ttl=1)
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=BALANCE) if calls == 1 else httpx.Response(403)

    with patch.object(collector, "_cookie", return_value="sample-session"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first = await smart.collect(client)
            smart.last_success_time = 0
            second = await smart.collect(client)
    assert first[0]["remaining"] == second[0]["remaining"] == "$140.00"
    assert smart.last_collection_state == "failed"
