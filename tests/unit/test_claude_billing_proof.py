"""What an account proves about its billing model, and where that proof may land.

The OAuth strategy has no Claude organization to read: the only thing that
knows how a claude.ai account bills is the shape of the cards it publishes.
These tests pin the hand-off from those cards to a billing model, and the
rules that keep a proof from claiming anything it cannot.
"""

from __future__ import annotations

from typing import Any

import pytest


def _oauth_response(payload: dict[str, Any]) -> Any:
    """An httpx-shaped response carrying a usage payload."""
    from unittest.mock import MagicMock

    import httpx

    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {}
    response.json.return_value = payload
    return response


def _oauth_client(payload: dict[str, Any]) -> Any:
    """An httpx-shaped client whose every request answers with the payload."""
    from unittest.mock import AsyncMock

    import httpx

    client = AsyncMock(spec=httpx.AsyncClient)
    client.request.return_value = _oauth_response(payload)
    return client


@pytest.mark.asyncio
async def test_the_oauth_strategy_publishes_the_model_its_cards_describe():
    """An OAuth account's own quota windows are the only proof it has."""
    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_cache import token_cache

    await token_cache.store(
        "anthropic",
        {"oauth_token": "sk-ant-oat-test"},  # pragma: allowlist secret
        account_id="default",
        source_id="source-a",
    )
    try:
        collector = AnthropicCollector()
        async with token_cache.using_source("anthropic", "default", "source-a"):
            cards = await collector._get_claude_oauth(
                _oauth_client({"five_hour": {"utilization": 25.0}}),
                "sk-ant-oat-test",  # pragma: allowlist secret
            )

        assert cards, "the strategy must publish its cards"
        # A claude.ai plan publishes percentage-of-limit windows: a subscription.
        assert collector.verified_billing_type == "subscription"
        # The proof is bound to the bundle that produced it.
        assert collector.verified_source_generation is not None
    finally:
        await token_cache.reset()


@pytest.mark.asyncio
async def test_a_response_that_decides_no_model_publishes_no_proof():
    """The absence of a proof is no decision."""
    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_cache import token_cache

    await token_cache.store(
        "anthropic",
        {"oauth_token": "sk-ant-oat-test"},  # pragma: allowlist secret
        account_id="default",
        source_id="source-a",
    )
    try:
        collector = AnthropicCollector()
        async with token_cache.using_source("anthropic", "default", "source-a"):
            # A payload whose limits are not limits: no card describes a plan.
            await collector._get_claude_oauth(
                _oauth_client({"limits": ["not-a-limit"]}),
                "sk-ant-oat-test",  # pragma: allowlist secret
            )

        assert collector.verified_billing_type is None
        assert getattr(collector, "verified_source_generation", None) is None
    finally:
        await token_cache.reset()


@pytest.mark.asyncio
async def test_a_bundle_replaced_mid_flight_publishes_no_proof():
    """A proof belongs to one credential, never to the one that replaced it."""
    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_cache import token_cache

    await token_cache.store(
        "anthropic",
        {"oauth_token": "sk-ant-oat-first"},  # pragma: allowlist secret
        account_id="default",
        source_id="source-a",
    )
    try:
        collector = AnthropicCollector()
        client = _oauth_client({"five_hour": {"utilization": 25.0}})
        response = client.request.return_value
        # The bundle is replaced while the answer is in flight.
        client.request.side_effect = None

        async def _replace_then_read(*args: Any, **kwargs: Any) -> Any:
            await token_cache.store(
                "anthropic",
                {"oauth_token": "sk-ant-oat-second"},  # pragma: allowlist secret
                account_id="default",
                source_id="source-a",
            )
            return response

        client.request.side_effect = _replace_then_read
        async with token_cache.using_source("anthropic", "default", "source-a"):
            await collector._get_claude_oauth(
                client,
                "sk-ant-oat-first",  # pragma: allowlist secret
            )

        assert collector.verified_billing_type is None
        assert getattr(collector, "verified_source_generation", None) is None
    finally:
        await token_cache.reset()


@pytest.mark.asyncio
async def test_a_credential_that_cannot_be_read_publishes_no_proof():
    """An expired token proves nothing about how the account bills."""
    from unittest.mock import AsyncMock, MagicMock

    import httpx

    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_cache import token_cache

    await token_cache.store(
        "anthropic",
        {"oauth_token": "sk-ant-oat-test"},  # pragma: allowlist secret
        account_id="default",
        source_id="source-a",
    )
    try:
        response = MagicMock(spec=httpx.Response)
        response.status_code = 401
        response.headers = {}
        client = AsyncMock(spec=httpx.AsyncClient)
        client.request.return_value = response

        collector = AnthropicCollector()
        async with token_cache.using_source("anthropic", "default", "source-a"):
            cards = await collector._get_claude_oauth(
                client,
                "sk-ant-oat-test",  # pragma: allowlist secret
            )

        # The failure is reported as a card, and it decides no model.
        assert cards and cards[0].get("error_type") == "auth_failed"
        assert collector.verified_billing_type is None
    finally:
        await token_cache.reset()


def test_the_oauth_cards_are_read_by_the_same_rule_everywhere():
    """One rule decides a model: the detector, wherever the cards came from."""
    from app.services.billing_type import UNKNOWN, detect_billing_type
    from app.services.claude_oauth_usage import billing_model_from_oauth_cards
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP

    cards = [
        {"unit_type": "currency", "currency": "USD"},
        {"used_value": 10.0, "limit_value": 100.0},
    ]
    assert billing_model_from_oauth_cards(cards) == detect_billing_type(cards)
    # A plan with prepaid credit on top is still a plan.
    assert billing_model_from_oauth_cards(cards) == "subscription"
    # Cards that say nothing about how an account bills decide nothing.
    assert billing_model_from_oauth_cards([{"unit_type": "token", "used_value": 10}]) is None
    assert billing_model_from_oauth_cards([]) is None
    assert detect_billing_type([]) == UNKNOWN
    # A published quota window is shaped like a plan's. A window the
    # collector invented is shaped the same way, which is why it has to say
    # so.
    window_cards = billing_model_from_oauth_cards(
        [
            {"used_value": 25.0, "limit_value": 100.0},
        ]
    )
    assert window_cards == "subscription"
    assert ANTHROPIC_WINDOW_NAME_MAP["five_hour"] == "Session"


@pytest.mark.asyncio
async def test_the_manual_path_prices_the_same_credential_the_same_way():
    """The wizard's detection and the strategy must not disagree."""
    from app.services.claude_oauth_usage import billing_model_from_oauth_usage

    model = await billing_model_from_oauth_usage(
        _oauth_client({"five_hour": {"utilization": 25.0}}),
        "sk-ant-oat-test",  # pragma: allowlist secret
    )
    assert model == "subscription"


@pytest.mark.asyncio
async def test_the_manual_path_claims_nothing_when_the_token_is_not_readable():
    """A token that cannot be read leaves the operator's choice untouched."""
    from unittest.mock import AsyncMock, MagicMock

    import httpx

    from app.services.claude_oauth_usage import billing_model_from_oauth_usage

    response = MagicMock(spec=httpx.Response)
    response.status_code = 401
    response.headers = {}
    client = AsyncMock(spec=httpx.AsyncClient)
    client.request.return_value = response

    assert (
        await billing_model_from_oauth_usage(
            client,
            "sk-ant-oat-test",  # pragma: allowlist secret
        )
        is None
    )


def test_a_balance_only_response_proves_no_model():
    """The collector's guaranteed windows are its own, not the account's."""
    from app.services.claude_oauth_usage import (
        billing_model_from_oauth_cards,
        oauth_usage_cards,
    )
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP

    cards = oauth_usage_cards(
        {"current_balance": 42.0},
        ANTHROPIC_WINDOW_NAME_MAP,
        None,
        data_source="api",
        input_source="config",
    )
    # Two windows the collector guaranteed, and both say they are placeholders.
    placeholders = [card for card in cards if card.get("billing_evidence") == "placeholder"]
    assert len(placeholders) == 2
    assert all(card["unit_type"] == "percent" for card in placeholders)
    # The balance is the account's own, and a balance is not a model.
    balances = [card for card in cards if card.get("billing_evidence") == "prepaid_credit"]
    assert len(balances) == 1
    assert balances[0]["unit_type"] == "currency"
    # So the whole response proves nothing.
    assert billing_model_from_oauth_cards(cards) is None


def test_a_published_quota_window_still_proves_a_subscription():
    """The account's own window decides; the collector's placeholder does not."""
    from app.services.claude_oauth_usage import (
        billing_model_from_oauth_cards,
        oauth_usage_cards,
    )
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP

    cards = oauth_usage_cards(
        {"five_hour": {"utilization": 25.0}},
        ANTHROPIC_WINDOW_NAME_MAP,
        None,
        data_source="api",
        input_source="config",
    )
    published = [card for card in cards if card.get("billing_evidence") != "placeholder"]
    assert len(published) == 1
    assert published[0]["used_value"] == 25.0
    # The weekly window the collector invented does not vote.
    assert len(cards) == 2
    assert billing_model_from_oauth_cards(cards) == "subscription"


def test_the_published_limits_still_prove_a_subscription():
    """A limits[] window is the account's own, and it is not marked."""
    from app.services.claude_oauth_usage import (
        billing_model_from_oauth_cards,
        oauth_usage_cards,
    )
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP

    cards = oauth_usage_cards(
        {
            "limits": [
                {
                    "kind": "session",
                    "group": "session",
                    "percent": 10,
                    "severity": "normal",
                    "resets_at": None,
                    "scope": {"model": None, "surface": None},
                    "is_active": True,
                }
            ]
        },
        ANTHROPIC_WINDOW_NAME_MAP,
        None,
        data_source="api",
        input_source="config",
    )
    assert cards
    assert all(card.get("billing_evidence") is None for card in cards)
    assert billing_model_from_oauth_cards(cards) == "subscription"


def test_a_claude_ai_balance_decides_nothing_whichever_strategy_read_it():
    """The web collector's balance is the same fact as the OAuth one."""
    from app.services.billing_type import UNKNOWN, detect_billing_type
    from app.services.collectors.anthropic import AnthropicCollector

    collector = AnthropicCollector(account_id="default")
    cards = collector._parse_web_api_response({"current_balance": 42.0})
    balances = [card for card in cards if card.get("unit_type") == "currency"]
    assert len(balances) == 1
    assert balances[0].get("billing_evidence") == "prepaid_credit"
    # So a web account that published only a balance proves no model.
    assert detect_billing_type(cards) == UNKNOWN


@pytest.mark.asyncio
async def test_the_manual_path_agrees_with_the_strategy_on_every_payload():
    """One rule, whichever path reads the credential."""
    from app.services.claude_oauth_usage import billing_model_from_oauth_usage

    payloads = (
        ({"current_balance": 42.0}, None),
        ({"five_hour": {"utilization": 25.0}}, "subscription"),
        (
            {
                "limits": [
                    {
                        "kind": "session",
                        "group": "session",
                        "percent": 10,
                        "severity": "normal",
                        "resets_at": None,
                        "scope": {"model": None, "surface": None},
                        "is_active": True,
                    }
                ]
            },
            "subscription",
        ),
    )
    for payload, expected in payloads:
        model = await billing_model_from_oauth_usage(
            _oauth_client(payload),
            "sk-ant-oat-test",  # pragma: allowlist secret
        )
        assert model == expected


def test_a_window_with_no_usable_number_proves_nothing():
    """The account published the window but no number in it."""
    from app.services.claude_oauth_usage import (
        billing_model_from_oauth_cards,
        oauth_usage_cards,
    )
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP

    for payload in ({"five_hour": {}}, {"five_hour": {"utilization": None}}):
        cards = oauth_usage_cards(
            payload,
            ANTHROPIC_WINDOW_NAME_MAP,
            None,
            data_source="api",
            input_source="config",
        )
        windows = [card for card in cards if card.get("window_type") == "session"]
        assert windows
        assert all(card.get("billing_evidence") == "placeholder" for card in windows)
        # The organization verifier calls this payload "Invalid quota" and
        # proves nothing, so the strategy must not price it either.
        assert billing_model_from_oauth_cards(cards) is None


def test_a_window_published_as_percent_used_still_proves_a_subscription():
    """percentUsed is the Web API's spelling; the OAuth path reads it too."""
    from app.services.claude_oauth_usage import (
        billing_model_from_oauth_cards,
        oauth_usage_cards,
    )
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP

    cards = oauth_usage_cards(
        {"five_hour": {"percentUsed": 41}},
        ANTHROPIC_WINDOW_NAME_MAP,
        None,
        data_source="api",
        input_source="config",
    )
    published = [card for card in cards if card.get("billing_evidence") != "placeholder"]
    assert len(published) == 1
    assert published[0]["used_value"] == 41.0
    assert billing_model_from_oauth_cards(cards) == "subscription"


def test_a_malformed_number_is_an_error_not_a_placeholder():
    """A corrupt quota is an error, not a missing one."""
    import math

    from app.services.claude_oauth_usage import oauth_usage_cards
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP

    # A non-numeric value raises, exactly as it did before: the collector
    # turns it into an error card.
    with pytest.raises(ValueError):
        oauth_usage_cards(
            {"five_hour": {"utilization": "invalid"}},
            ANTHROPIC_WINDOW_NAME_MAP,
            None,
            data_source="api",
            input_source="config",
        )

    # A non-finite value stays non-finite, so the verification layer can
    # reject the payload instead of the collector storing a 0 % window.
    cards = oauth_usage_cards(
        {"five_hour": {"utilization": float("nan")}},
        ANTHROPIC_WINDOW_NAME_MAP,
        None,
        data_source="api",
        input_source="config",
    )
    assert math.isnan(cards[0]["used_value"])
    assert "billing_evidence" not in cards[0]


def test_a_limits_entry_with_no_number_proves_nothing():
    """A limits[] entry that names a window but carries no percentage."""
    from app.services.claude_oauth_usage import (
        billing_model_from_oauth_cards,
        oauth_usage_cards,
    )
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP

    cards = oauth_usage_cards(
        {
            "limits": [
                {
                    "kind": "session",
                    "group": "session",
                    "severity": "normal",
                    "resets_at": None,
                    "scope": {"model": None, "surface": None},
                    "is_active": True,
                }
            ]
        },
        ANTHROPIC_WINDOW_NAME_MAP,
        None,
        data_source="api",
        input_source="config",
    )
    assert cards
    assert all(card.get("billing_evidence") == "placeholder" for card in cards)
    assert billing_model_from_oauth_cards(cards) is None


def test_a_web_window_with_no_usable_number_decides_nothing():
    """The web collector draws the same rule as the OAuth one."""
    from app.services.billing_type import UNKNOWN, detect_billing_type
    from app.services.collectors.anthropic import AnthropicCollector

    collector = AnthropicCollector(account_id="default")
    cards = collector._parse_web_api_response({"five_hour": {}, "current_balance": 42.0})
    windows = [card for card in cards if card.get("unit_type") == "percent"]
    assert windows
    assert all(card.get("billing_evidence") == "placeholder" for card in windows)
    # Nothing in that payload is evidence, so nothing is decided.
    assert detect_billing_type(cards) == UNKNOWN


def test_a_boolean_percentage_is_refused_not_read_as_one_percent():
    """``float(True)`` is 1.0, so a boolean would invent a percentage."""
    from app.services.claude_oauth_usage import oauth_usage_cards
    from app.services.collectors._anthropic_common import (
        ANTHROPIC_WINDOW_NAME_MAP,
        anthropic_published_percent,
    )

    with pytest.raises(ValueError):
        anthropic_published_percent({"utilization": True}, "utilization")

    # The window is published, but the number in it is not a number: the
    # collector refuses it rather than reading it as the 1 % it would become.
    with pytest.raises(ValueError):
        oauth_usage_cards(
            {"five_hour": {"utilization": True}},
            ANTHROPIC_WINDOW_NAME_MAP,
            None,
            data_source="api",
            input_source="config",
        )


def test_a_boolean_in_a_limits_entry_is_refused():
    """The limits[] path reads its percentage through the same rule."""
    from app.services.claude_oauth_usage import oauth_usage_cards
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP

    with pytest.raises(ValueError):
        oauth_usage_cards(
            {
                "limits": [
                    {
                        "kind": "session",
                        "group": "session",
                        "percent": True,
                        "severity": "normal",
                        "resets_at": None,
                        "scope": {"model": None, "surface": None},
                        "is_active": True,
                    }
                ]
            },
            ANTHROPIC_WINDOW_NAME_MAP,
            None,
            data_source="api",
            input_source="config",
        )


@pytest.mark.asyncio
async def test_a_boolean_decides_no_model_on_any_path():
    """No path may read a boolean as the percentage that decides a model."""
    from app.services.billing_type import UNKNOWN, detect_billing_type
    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_cache import token_cache

    collector = AnthropicCollector(account_id="default")

    # The Web path: the corrupt quota is an error card, not a 1 % window.
    web_cards = collector._parse_web_usage({"five_hour": {"utilization": True}})
    assert web_cards
    assert all(card.get("error_type") == "parse_error" for card in web_cards)
    assert detect_billing_type(web_cards) == UNKNOWN

    # The OAuth strategy: the same payload is an error card there too, and it
    # publishes no proof.
    await token_cache.store(
        "anthropic",
        {"oauth_token": "sk-ant-oat-test"},  # pragma: allowlist secret
        account_id="default",
        source_id="source-a",
    )
    try:
        strategy = AnthropicCollector()
        async with token_cache.using_source("anthropic", "default", "source-a"):
            cards = await strategy._get_claude_oauth(
                _oauth_client({"five_hour": {"utilization": True}}),
                "sk-ant-oat-test",  # pragma: allowlist secret
            )
        assert cards
        assert all(card.get("error_type") == "parse_error" for card in cards)
        assert strategy.verified_billing_type is None
    finally:
        await token_cache.reset()


def test_a_boolean_money_field_is_refused():
    """A boolean is not a balance or a limit either."""
    from app.services.claude_oauth_usage import oauth_usage_cards
    from app.services.collectors._anthropic_common import ANTHROPIC_WINDOW_NAME_MAP
    from app.services.collectors.anthropic import AnthropicCollector

    # A balance the account did not publish is no balance: the card is skipped
    # rather than invented as $1.00.
    cards = oauth_usage_cards(
        {"current_balance": True},
        ANTHROPIC_WINDOW_NAME_MAP,
        None,
        data_source="api",
        input_source="config",
    )
    assert not [card for card in cards if card.get("unit_type") == "currency"]

    # A limit that is a boolean would decide pay-as-you-go on a number the
    # account never published, so it refuses instead.
    with pytest.raises(ValueError):
        oauth_usage_cards(
            {"five_hour": {"limit": True, "spend": 0}},
            ANTHROPIC_WINDOW_NAME_MAP,
            None,
            data_source="api",
            input_source="config",
        )

    # The web collector reads the same fields the same way.
    collector = AnthropicCollector(account_id="default")
    web_cards = collector._parse_web_usage({"current_balance": True})
    assert not [card for card in web_cards if card.get("unit_type") == "currency"]

    web_limit = collector._parse_web_usage({"extra_usage": {"limit": True, "spend": 0}})
    assert not [card for card in web_limit if card.get("unit_type") == "currency"]


@pytest.mark.asyncio
async def test_the_manual_pricing_path_survives_a_corrupt_quota():
    """The preview answers a question; a quota it cannot read proves nothing."""
    from app.services.claude_oauth_usage import billing_model_from_oauth_usage

    for payload in (
        {"five_hour": {"utilization": True}},
        {"five_hour": {"utilization": "invalid"}},
        {"five_hour": {"limit": True, "spend": 0}},
    ):
        model = await billing_model_from_oauth_usage(
            _oauth_client(payload),
            "sk-ant-oat-test",  # pragma: allowlist secret
        )
        assert model is None
