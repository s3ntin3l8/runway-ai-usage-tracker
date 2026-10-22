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
                client, "sk-ant-oat-first"
            )  # pragma: allowlist secret

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
                client, "sk-ant-oat-test"
            )  # pragma: allowlist secret

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
    # The collector always publishes a quota window, so its cards are shaped
    # like a plan's.
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
        await billing_model_from_oauth_usage(client, "sk-ant-oat-test") is None
    )  # pragma: allowlist secret
