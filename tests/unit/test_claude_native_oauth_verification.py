"""Native OAuth must prove holder and quota for the exact pinned generation."""

import httpx
import pytest

from app.services.collectors.anthropic import AnthropicCollector
from app.services.token_cache import token_cache

ACCOUNT = "11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_status,quota_status,binding,success",
    [
        (200, 200, ACCOUNT, True),
        (403, 200, ACCOUNT, False),
        (200, 403, ACCOUNT, False),
        (200, 429, ACCOUNT, False),
        (200, 503, ACCOUNT, False),
        (200, 200, "22222222-2222-4222-8222-222222222222", False),
    ],
)
async def test_native_oauth_requires_profile_binding_and_quota(
    profile_status, quota_status, binding, success
):
    tokens = {"oauth_token": "native-access", "oauth_read_only": "1", "oauth_account_uuid": binding}
    await token_cache.store(
        "anthropic",
        tokens,
        account_id="source",
        source_id="source",
        source_metadata={
            "identity_pending": True,
            "credential_origin": "keychain:Claude Desktop/oauth/slot",
        },
    )
    paths = []

    def upstream(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        if request.url.path.endswith("/profile"):
            return httpx.Response(
                profile_status, json={"account": {"uuid": ACCOUNT, "email": "holder@example.com"}}
            )
        assert request.url.path.endswith("/usage")
        return httpx.Response(
            quota_status, json={"five_hour": {"utilization": 20}, "seven_day": {"utilization": 30}}
        )

    collector = AnthropicCollector(account_id="default")
    async with token_cache.using_source("anthropic", "source", "source"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            cards = await collector._strategy_oauth_wrap(client)
    usable = [c for c in cards if not c.get("error_type") and c.get("remaining") != "ERR"]
    assert bool(usable) is success
    assert collector.account_id == ("holder@example.com" if success else "default")
    if success:
        assert collector.verified_source_generation == token_cache.credential_generation(tokens)
    if profile_status != 200 or binding != ACCOUNT:
        assert paths == ["/api/oauth/profile"]


@pytest.mark.asyncio
async def test_oauth_rotation_during_verification_cannot_publish():
    tokens = {"oauth_token": "old-access", "oauth_read_only": "1", "oauth_account_uuid": ACCOUNT}
    await token_cache.store(
        "anthropic",
        tokens,
        account_id="source",
        source_id="source",
        source_metadata={"identity_pending": True},
    )

    async def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(
                200, json={"account": {"uuid": ACCOUNT, "email": "holder@example.com"}}
            )
        await token_cache.store(
            "anthropic",
            {**tokens, "oauth_token": "replacement"},
            account_id="source",
            source_id="source",
            source_metadata={"identity_pending": True},
        )
        return httpx.Response(200, json={"five_hour": {"utilization": 20}})

    collector = AnthropicCollector(account_id="default")
    async with token_cache.using_source("anthropic", "source", "source"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            cards = await collector._strategy_oauth_wrap(client)
    assert not [c for c in cards if not c.get("error_type")]
    assert collector.account_id == "default"


@pytest.mark.asyncio
async def test_read_only_oauth_never_uses_refresh_even_when_expired():
    await token_cache.store(
        "anthropic",
        {
            "oauth_token": "expired",
            "refresh_token": "must-not-use",
            "expiry_date": "1",
            "oauth_read_only": "1",
        },
        account_id="source",
        source_id="source",
        source_metadata={"identity_pending": True},
    )

    def upstream(request):
        pytest.fail("Expired read-only source must not issue any request")

    collector = AnthropicCollector(account_id="default")
    async with token_cache.using_source("anthropic", "source", "source"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            cards = await collector._strategy_oauth_wrap(client)
    assert not [c for c in cards if not c.get("error_type")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        {"limits": [{}]},
        {"limits": [{"percent": "nan"}]},
        {"limits": [{"percent": float("inf")}]},
        {"five_hour": {"utilization": 20}, "seven_day": {"utilization": "invalid"}},
        {"limits": [{"percent": 20, "scope": 42}]},
        {"five_hour": {"utilization": 20}, "extra_usage": {"utilization": "nan"}},
    ],
)
async def test_malformed_native_quota_cannot_become_verified_snapshot(usage):
    tokens = {"oauth_token": "access", "oauth_read_only": "1"}
    await token_cache.store(
        "anthropic",
        tokens,
        account_id="source",
        source_id="source",
        source_metadata={"identity_pending": True},
    )

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"account": {"email": "holder@example.com"}})
        # Explicit serialization permits testing nonfinite data from an upstream.
        import json

        return httpx.Response(
            200, content=json.dumps(usage).encode(), headers={"content-type": "application/json"}
        )

    collector = AnthropicCollector(account_id="default")
    async with token_cache.using_source("anthropic", "source", "source"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            cards = await collector._strategy_oauth_wrap(client)
    assert not [c for c in cards if not c.get("error_type")]
    assert collector.account_id == "default"
    assert getattr(collector, "verified_source_generation", None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile,quota",
    [
        ({}, {"five_hour": {"utilization": 20}}),
        ({"account": {"email": "not-an-email"}}, {"five_hour": {"utilization": 20}}),
        (
            {"account": {"email": "person@example.com", "uuid": "invalid"}},
            {"five_hour": {"utilization": 20}},
        ),
        ({"account": {"email": "person@example.com", "uuid": ACCOUNT}}, []),
        (
            {"account": {"email": "person@example.com", "uuid": ACCOUNT}},
            {"limits": [{"percent": 20, "group": "weekly", "scope": {"model": 42}}]},
        ),
    ],
)
async def test_unsupported_profile_or_quota_degrades_without_identity_or_proof(profile, quota):
    await token_cache.store(
        "anthropic",
        {"oauth_token": "access", "oauth_read_only": "1", "oauth_account_uuid": ACCOUNT},
        account_id="source",
        source_id="source",
        source_metadata={"identity_pending": True},
    )

    def upstream(request):
        return httpx.Response(200, json=profile if request.url.path.endswith("/profile") else quota)

    collector = AnthropicCollector(account_id="default")
    async with token_cache.using_source("anthropic", "source", "source"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            cards = await collector._strategy_oauth_wrap(client)
    assert cards and all(c.get("error_type") for c in cards)
    assert collector.account_id == "default"
    assert getattr(collector, "verified_source_generation", None) is None


@pytest.mark.asyncio
async def test_native_oauth_transport_failure_is_sanitized_and_does_not_refresh():
    await token_cache.store(
        "anthropic",
        {"oauth_token": "private-access", "oauth_read_only": "1"},
        account_id="source",
        source_id="source",
        source_metadata={"identity_pending": True},
    )

    def upstream(request):
        raise httpx.ReadTimeout("private-access", request=request)

    collector = AnthropicCollector(account_id="default")
    async with token_cache.using_source("anthropic", "source", "source"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            cards = await collector._strategy_oauth_wrap(client)
    assert cards[0]["error_type"] == "timeout"
    assert "private-access" not in repr(cards)
    assert collector.account_id == "default"


def test_desktop_oauth_has_an_app_specific_label_and_login_hint():
    from app.services.credential_sources import describe_origin_full, login_hint

    display = describe_origin_full("keychain:Claude Desktop/oauth/slot")
    assert display.app == "Claude Desktop"
    assert display.kind == "keychain"
    assert "Desktop" in login_hint(display.app)
