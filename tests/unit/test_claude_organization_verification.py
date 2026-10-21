"""Exact cookie sources must prove an unambiguous usable Claude organization."""

import httpx
import pytest
from sqlmodel import Session as RealSession

ORG_A = "11111111-1111-4111-8111-111111111111"
ORG_B = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_status,complete,count,selected",
    [(200, True, 2, None), (403, True, 1, ORG_A), (503, False, 1, None)],
)
async def test_subscription_checks_each_org_before_selecting(
    second_status, complete, count, selected
):
    from app.services.claude_verification import verify_claude_cookie

    def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200, json=[{"uuid": ORG_A, "name": "A"}, {"uuid": ORG_B, "name": "B"}]
            )
        status = second_status if ORG_B in request.url.path else 200
        return httpx.Response(status, json={"five_hour": {"utilization": 10}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await verify_claude_cookie(client, "anthropic", "test-cookie")
    assert result.complete is complete
    assert len(result.organizations) == count
    assert result.organization_id == selected
    assert result.account_id == "person@example.com"


@pytest.mark.asyncio
async def test_selection_must_be_accessible_to_exact_cookie():
    from app.services.claude_verification import verify_claude_cookie

    def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": ORG_A, "name": "A"}])
        return httpx.Response(200, json={"five_hour": {"utilization": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await verify_claude_cookie(client, "anthropic", "test-cookie", ORG_B)
    assert result.organization_id is None


def test_claude_cookie_origins_never_inherit_base_tags():
    from app.services.credential_tags import origin_candidates

    origin = "cookie:anthropic/chrome/Default/claude.ai#abcdef123456"
    assert origin_candidates(origin) == [origin]


def test_console_pending_sources_have_separate_slots():
    from app.services.credential_sources import pending_cache_slot

    assert pending_cache_slot("anthropic_console", "source-a") == "source-a"
    assert pending_cache_slot("anthropic_console", "source-b") == "source-b"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,content_type,expected",
    [
        ("/api/organizations/" + ORG_A + "/usage", "application/json", False),
        ("/api/account", "text/html", False),
        ("/api/account", "application/json", True),
    ],
)
async def test_auth_hook_distinguishes_org_denial_and_browser_challenge(
    path, content_type, expected
):
    from unittest.mock import patch

    from app.services.token_cache import token_cache

    request = httpx.Request(
        "GET", "https://claude.ai" + path, headers={"cookie": "sessionKey=test-cookie"}
    )
    response = httpx.Response(403, headers={"content-type": content_type}, request=request)
    async with token_cache.using_source("anthropic", "default", "source-a") as attempt:
        with patch.object(token_cache, "_request_uses_active_source", return_value=True):
            await token_cache.observe_response(response)
        assert attempt["auth_failed"] is expected


@pytest.fixture
def claude_db(tmp_path, monkeypatch):
    from sqlmodel import SQLModel, create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path / 'claude.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("sqlmodel.Session", RealSession)
    monkeypatch.setattr("app.core.db.engine", engine)
    yield engine
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("count,expected_id", [(1, "person@example.com"), (2, "default")])
async def test_native_subscription_collector_promotes_only_verified_selection(
    claude_db, count, expected_id
):
    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_cache import token_cache

    await token_cache.store(
        "anthropic",
        {"cookie_sessionKey": "test-cookie"},
        account_id="source-a",
        source_id="source-a",
        source_metadata={
            "identity_pending": True,
            "credential_origin": "cookie:anthropic/chrome/Default/claude.ai#abcdef123456",
        },
    )

    def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200, json=[{"uuid": oid, "name": "Org"} for oid in [ORG_A, ORG_B][:count]]
            )
        return httpx.Response(200, json={"five_hour": {"utilization": 10}})

    collector = AnthropicCollector(account_id="default")
    async with token_cache.using_source("anthropic", "source-a", "source-a"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await collector._get_claude_via_web_api(client)
    assert collector.account_id == expected_id
    assert bool(result) is (count == 1)
    if count == 1:
        assert collector.verified_organization_id == ORG_A
        assert result[0]["data_source"] == "web"
        assert result[0]["input_source"] == "sidecar"


@pytest.mark.asyncio
async def test_source_organization_endpoint_rejects_unknown_source(claude_db):
    from fastapi import HTTPException
    from sqlmodel import Session

    from app.api.endpoints.fleet import get_claude_source_organizations

    with Session(claude_db) as session:
        with pytest.raises(HTTPException) as exc:
            await get_claude_source_organizations("sidecar:missing", "anthropic", session, None)
    assert exc.value.status_code == 404


async def seed_claude_source(engine, provider="anthropic"):
    from app.models.db import CredentialSource, PendingCredentialTag
    from app.services.credential_sources import sidecar_source_id
    from app.services.token_cache import token_cache

    origin = f"cookie:{provider}/chrome/Default/store#abcdef123456"
    source_id = sidecar_source_id("host-a", origin)
    with RealSession(engine) as session:
        session.add(
            PendingCredentialTag(
                sidecar_id="host-a", provider_id=provider, credential_origin=origin
            )
        )
        session.add(
            CredentialSource(
                provider_id=provider,
                account_id=source_id,
                source_id=source_id,
                source_type="cookie",
                source_label="host-a",
                sidecar_id="host-a",
                credential_origin=origin,
            )
        )
        session.commit()
    await token_cache.store(
        provider,
        {"cookie_sessionKey": "test-cookie"}
        if provider == "anthropic"
        else {"session_cookie": "test-cookie"},
        account_id=source_id,
        source_id=source_id,
        source="host-a",
        source_metadata={
            "identity_pending": True,
            "sidecar_id": "host-a",
            "credential_origin": origin,
            "source_type": "cookie",
        },
    )
    return source_id


@pytest.mark.asyncio
async def test_organization_picker_persists_exact_source_binding(claude_db, monkeypatch):
    from sqlmodel import select

    from app.api.endpoints.fleet import (
        ClaudeOrganizationSelection,
        get_claude_source_organizations,
        select_claude_source_organization,
    )
    from app.models.db import CredentialSource, PendingCredentialTag, ProviderConfig

    source_id = await seed_claude_source(claude_db)

    def upstream(request):
        assert request.headers["cookie"] == "sessionKey=test-cookie"
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200,
                json=[{"uuid": oid, "name": name} for oid, name in [(ORG_A, "A"), (ORG_B, "B")]],
            )
        return httpx.Response(200, json={"five_hour": {"utilization": 10}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    with RealSession(claude_db) as session:
        result = await get_claude_source_organizations(source_id, "anthropic", session, None)
        assert result["selection_required"] is True
        assert result["complete"] is True
        await select_claude_source_organization(
            source_id,
            ClaudeOrganizationSelection(provider_id="anthropic", organization_id=ORG_B),
            session,
            None,
        )
    with RealSession(claude_db) as session:
        config = session.exec(select(ProviderConfig)).one()
        assert config.account_id == "person@example.com"
        assert config.claude_organization_id == ORG_B
        assert session.exec(select(CredentialSource)).one().account_id == "person@example.com"
        assert session.exec(select(PendingCredentialTag)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,organization,status",
    [
        ("anthropic_console", ORG_A, 404),
        ("anthropic", "not-a-uuid", 422),
        ("anthropic", ORG_B, 403),
    ],
)
async def test_selection_rejects_wrong_provider_malformed_or_unavailable_org(
    claude_db, monkeypatch, provider, organization, status
):
    from fastapi import HTTPException

    from app.api.endpoints.fleet import (
        ClaudeOrganizationSelection,
        select_claude_source_organization,
    )

    source_id = await seed_claude_source(claude_db)

    def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": ORG_A, "name": "A"}])
        return httpx.Response(200, json={"five_hour": {"utilization": 10}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    with RealSession(claude_db) as session:
        with pytest.raises(HTTPException) as exc:
            await select_claude_source_organization(
                source_id,
                ClaudeOrganizationSelection(provider_id=provider, organization_id=organization),
                session,
                None,
            )
    assert exc.value.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_organization_source_endpoints_require_admin(method, monkeypatch):
    from app.core.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "test-admin")
    monkeypatch.setattr("app.core.security.settings.ADMIN_API_KEY", "test-admin")
    monkeypatch.setattr("app.core.security.settings.APP_HOST", "0.0.0.0")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.request(
            method,
            "/api/v1/fleet/credentials/sidecar:missing/claude-organizations",
            params={"provider_id": "anthropic"},
            **(
                {"json": {"provider_id": "anthropic", "organization_id": ORG_A}}
                if method == "POST"
                else {}
            ),
        )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_picker_rejects_missing_durable_source(claude_db):
    from fastapi import HTTPException
    from sqlmodel import select

    from app.api.endpoints.fleet import _pending_claude_source
    from app.models.db import CredentialSource

    source_id = await seed_claude_source(claude_db)
    with RealSession(claude_db) as session:
        session.delete(session.exec(select(CredentialSource)).one())
        session.commit()
        with pytest.raises(HTTPException) as exc:
            await _pending_claude_source(session, "anthropic", source_id)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_claude_verification_never_follows_redirect_even_if_client_does():
    from app.services.claude_verification import verify_claude_cookie
    from app.services.collectors.anthropic_console import ConsoleAPIError

    seen = []

    def upstream(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/login"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), follow_redirects=True
    ) as client:
        with pytest.raises(ConsoleAPIError):
            await verify_claude_cookie(client, "anthropic", "test-cookie")
    assert seen == ["https://claude.ai/api/account"]


@pytest.mark.asyncio
async def test_pending_source_respects_existing_verified_accounts_organization(claude_db):
    from app.models.db import ProviderConfig
    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_cache import token_cache

    source_id = await seed_claude_source(claude_db)
    with RealSession(claude_db) as session:
        session.add(
            ProviderConfig(
                provider_id="anthropic",
                account_id="person@example.com",
                claude_organization_id=ORG_B,
            )
        )
        session.commit()

    def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200, json=[{"uuid": oid, "name": "Org"} for oid in (ORG_A, ORG_B)]
            )
        return httpx.Response(200, json={"five_hour": {"utilization": 10}})

    collector = AnthropicCollector(account_id="default")
    async with token_cache.using_source("anthropic", source_id, source_id):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await collector._get_claude_via_web_api(client)
    assert result
    assert collector.verified_organization_id == ORG_B


@pytest.mark.asyncio
async def test_native_console_sources_prove_their_own_organization_uuid(claude_db):
    from app.services.collectors.anthropic_console import AnthropicConsoleCollector
    from app.services.token_cache import token_cache

    source_id = await seed_claude_source(claude_db, "anthropic_console")

    def upstream(request):
        assert request.url.host == "platform.claude.com"
        assert request.headers["cookie"] == "sessionKey=test-cookie"
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200, json=[{"uuid": ORG_A, "name": "API", "capabilities": ["api"]}]
            )
        return httpx.Response(200, json={"amount": 1234, "currency": "USD"})

    collector = AnthropicConsoleCollector(account_id="default")
    async with token_cache.using_source("anthropic_console", source_id, source_id):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await collector._primary_strategy(client)
    assert collector.account_id == ORG_A
    assert collector.verified_identity == ORG_A
    assert result[0]["remaining"] == "$12.34"


def test_cookie_rotation_cannot_copy_prior_fingerprint_binding(claude_db):
    from app.services.credential_tags import CredentialTagRepo

    base = "cookie:anthropic/chrome/Default/store"
    with RealSession(claude_db) as session:
        CredentialTagRepo.set_tag(
            session,
            provider_id="anthropic",
            credential_origin=base,
            account_id="old@example.com",
            sidecar_id="host-a",
            set_by="operator",
        )
        CredentialTagRepo.set_tag(
            session,
            provider_id="anthropic",
            credential_origin=base + "#aaaaaaaaaaaa",
            account_id="old@example.com",
            sidecar_id="host-a",
            set_by="operator",
        )
        session.commit()
        assert (
            CredentialTagRepo.get(
                session,
                provider_id="anthropic",
                credential_origin=base + "#bbbbbbbbbbbb",
                sidecar_id="host-a",
            )
            is None
        )
        assert (
            CredentialTagRepo.inherited_account_for_rotation(
                session,
                provider_id="anthropic",
                credential_origin=base + "#bbbbbbbbbbbb",
                sidecar_id="host-a",
            )
            is None
        )


@pytest.mark.asyncio
async def test_rotated_cookie_cannot_inherit_inflight_verification(claude_db):
    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_cache import token_cache

    source_id = await seed_claude_source(claude_db)

    async def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": ORG_A, "name": "Org"}])
        await token_cache.store(
            "anthropic",
            {"cookie_sessionKey": "different-cookie"},
            account_id=source_id,
            source_id=source_id,
            source_metadata={"identity_pending": True},
        )
        return httpx.Response(200, json={"five_hour": {"utilization": 10}})

    collector = AnthropicCollector(account_id="default")
    async with token_cache.using_source("anthropic", source_id, source_id):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await collector._get_claude_via_web_api(client)
    assert result == []
    assert collector.account_id == "default"


@pytest.mark.asyncio
async def test_invalid_quota_cannot_make_an_organization_eligible():
    from app.services.claude_verification import verify_claude_cookie

    def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": ORG_A, "name": "Org"}])
        return httpx.Response(200, json={"five_hour": {"utilization": "not-a-number"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await verify_claude_cookie(client, "anthropic", "test-cookie")
    assert result.organization_id is None
    assert result.complete is False


@pytest.mark.asyncio
async def test_completed_manifest_retires_absent_cookie_but_keeps_same_store_siblings(claude_db):
    from app.services.credential_sources import retire_missing_claude_cookie_sources

    source_id = await seed_claude_source(claude_db)
    from sqlmodel import select

    from app.models.db import CredentialSource

    with RealSession(claude_db) as session:
        source = session.exec(select(CredentialSource)).one()
        sibling = CredentialSource(
            provider_id="anthropic",
            account_id="sibling",
            source_id="sibling",
            source_type="cookie",
            source_label="host-a",
            sidecar_id="host-a",
            credential_origin=source.credential_origin.replace("abcdef123456", "bbbbbbbbbbbb"),
        )
        session.add(sibling)
        session.commit()
        # An incomplete provider never retires an unseen source.
        assert (
            retire_missing_claude_cookie_sources(
                session, sidecar_id="host-a", completed_providers=[], keep_by_provider={}
            )
            == []
        )
        # A legacy unkeyed observation cannot prove an exact-cookie replacement.
        assert (
            retire_missing_claude_cookie_sources(
                session,
                sidecar_id="host-a",
                completed_providers=["anthropic"],
                keep_by_provider={"anthropic": {source.credential_origin.split("#", 1)[0]}},
            )
            == []
        )
        retired = retire_missing_claude_cookie_sources(
            session,
            sidecar_id="host-a",
            completed_providers=["anthropic"],
            keep_by_provider={"anthropic": {sibling.credential_origin}},
        )
        assert [row.source_id for row in retired] == [source_id]
        session.commit()
        assert session.exec(select(CredentialSource)).one().source_id == "sibling"


@pytest.mark.asyncio
async def test_reconciling_already_promoted_source_keeps_active_account():
    from app.services.token_cache import token_cache

    await token_cache.store(
        "anthropic",
        {"cookie_sessionKey": "test-cookie"},
        account_id="source-a",
        source_id="source-a",
        source_metadata={"identity_pending": True},
    )
    assert await token_cache.move_source("anthropic", "source-a", "person@example.com", "source-a")
    assert await token_cache.move_source(
        "anthropic", "person@example.com", "person@example.com", "source-a"
    )
    assert any(
        provider == "anthropic" and account == "person@example.com"
        for provider, account, _label in await token_cache.get_all_active_accounts()
    )


@pytest.mark.asyncio
async def test_manual_cookie_fallback_honors_saved_subscription_organization(claude_db):
    from app.models.db import ProviderConfig
    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_cache import token_cache

    with RealSession(claude_db) as session:
        session.add(
            ProviderConfig(
                provider_id="anthropic",
                account_id="person@example.com",
                claude_organization_id=ORG_B,
            )
        )
        session.commit()
    await token_cache.store(
        "anthropic",
        {"cookie_sessionKey": "manual-cookie"},
        account_id="person@example.com",
        source_id="manual",
        source_metadata={"source_type": "config"},
    )

    def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200, json=[{"uuid": oid, "name": "Org"} for oid in (ORG_A, ORG_B)]
            )
        return httpx.Response(
            200, json={"five_hour": {"utilization": 90 if ORG_B in request.url.path else 10}}
        )

    collector = AnthropicCollector(account_id="person@example.com")
    async with token_cache.using_source("anthropic", "person@example.com", "manual"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await collector._get_claude_via_web_api(client)
    assert result[0]["used_value"] == 90
    assert collector.verified_organization_id == ORG_B


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata,count",
    [
        ("{never-output-secret", 0),
        ('{"secret":"never-output-secret"}', 0),  # pragma: allowlist secret - synthetic
        ('["never-output-secret",7]', 0),
        ("null", 0),
        ('[{"id":"one","name":"A"},{"id":"two","name":"B"}]', 2),
    ],
)
async def test_pending_organization_metadata_is_robust_and_not_returned(claude_db, metadata, count):
    from app.api.endpoints.fleet import list_pending_credential_tags
    from app.models.db import PendingCredentialTag

    with RealSession(claude_db) as session:
        session.add(
            PendingCredentialTag(
                sidecar_id="host-a",
                provider_id="anthropic",
                credential_origin="cookie:anthropic/chrome/Default/store#aaaaaaaaaaaa",
                claude_organizations_json=metadata,
            )
        )
        session.commit()
        result = await list_pending_credential_tags(None, session, None)
    assert len(result["items"]) == 1
    assert result["items"][0]["organization_count"] == count
    assert result["items"][0]["selection_required"] is (count > 1)
    import json

    assert "never-output-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_verified_subscription_account_is_named_after_its_member(claude_db):
    from app.services.claude_verification import verify_claude_cookie

    def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": ORG_A, "name": "Org"}])
        return httpx.Response(200, json={"five_hour": {"utilization": 10}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await verify_claude_cookie(client, "anthropic", "test-cookie")
    assert result.account_label == "person@example.com"
    # A plan publishes percentage quotas, so it bills by subscription.
    assert result.billing_type == "subscription"


@pytest.mark.asyncio
async def test_verified_console_account_is_named_after_its_organization(claude_db):
    from app.services.claude_verification import verify_claude_cookie

    def upstream(request):
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200, json=[{"uuid": ORG_A, "name": "API org", "capabilities": ["api"]}]
            )
        return httpx.Response(200, json={"amount": 1234, "currency": "USD"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await verify_claude_cookie(client, "anthropic_console", "test-cookie")
    # Console credit is shared by the whole organization, so the organization names it.
    assert result.account_label == "API org"
    # A prepaid balance is spend, not a quota.
    assert result.billing_type == "pay_as_you_go"


@pytest.mark.asyncio
async def test_console_cards_carry_the_organization_name(claude_db):
    """The Console account must not render as an unnamed "Default" account."""
    from app.services.collectors.anthropic_console import AnthropicConsoleCollector
    from app.services.token_cache import token_cache

    source_id = await seed_claude_source(claude_db, "anthropic_console")

    def upstream(request):
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200, json=[{"uuid": ORG_A, "name": "API org", "capabilities": ["api"]}]
            )
        return httpx.Response(200, json={"amount": 1234, "currency": "USD"})

    collector = AnthropicConsoleCollector(account_id="default")
    async with token_cache.using_source("anthropic_console", source_id, source_id):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await collector._primary_strategy(client)
    assert collector.account_label == "API org"
    assert collector.verified_billing_type == "pay_as_you_go"
    assert result[0]["remaining"] == "$12.34"


@pytest.mark.asyncio
async def test_console_credit_never_borrows_the_signed_in_members_email():
    """Two members of one organization share the account, so the label must not flap."""
    from app.services.claude_verification import verify_claude_cookie

    def upstream(request):
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200,
                json=[
                    {
                        "uuid": ORG_A,
                        "name": "API org",
                        "capabilities": ["api"],
                        "membership": {"user": {"email": "member@example.com"}},
                    }
                ],
            )
        return httpx.Response(200, json={"amount": 1234, "currency": "USD"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await verify_claude_cookie(client, "anthropic_console", "test-cookie")
    assert result.account_label == "API org"


@pytest.mark.asyncio
async def test_console_card_keeps_the_name_the_operator_chose(claude_db):
    """A renamed Console account must not wear the organization's name again."""
    from app.services.collectors.anthropic_console import AnthropicConsoleCollector
    from app.services.token_cache import token_cache

    source_id = await seed_claude_source(claude_db, "anthropic_console")

    def upstream(request):
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200, json=[{"uuid": ORG_A, "name": "API org", "capabilities": ["api"]}]
            )
        return httpx.Response(200, json={"amount": 1234, "currency": "USD"})

    collector = AnthropicConsoleCollector(account_id="default", account_label="Team wallet")
    async with token_cache.using_source("anthropic_console", source_id, source_id):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            result = await collector._primary_strategy(client)
    assert collector.account_label == "Team wallet"
    assert collector._tag_results(result)[0]["account_label"] == "Team wallet"


@pytest.mark.asyncio
async def test_subscription_card_keeps_the_name_the_operator_chose():
    """A renamed subscription account must not revert to the member's email."""
    from unittest.mock import AsyncMock, patch

    from app.services.collectors.anthropic import AnthropicCollector

    collector = AnthropicCollector(account_id="default", account_label="Team wallet")
    with patch(
        "app.services.collectors.anthropic_web.token_cache.update_account_metadata",
        AsyncMock(),
    ):
        result = collector._parse_web_api_response(
            {"five_hour": {"utilization": 10}},
            org_data={"membership": {"user": {"email": "member@example.com"}}},
        )
    # The collector keeps the name the operator chose.
    assert collector.account_label == "Team wallet"
    # The card still reports the identity that keys it: ``anthropic`` is not a
    # login-keyed provider, so an email-shaped card label is what files the
    # card under its account.
    assert all(card.get("account_label") == "member@example.com" for card in result)


@pytest.mark.asyncio
async def test_a_placeholder_name_is_filled_in_on_the_subscription_path():
    """The placeholder is a placeholder in any spelling, so it may be filled in."""
    from unittest.mock import AsyncMock, patch

    from app.services.collectors.anthropic import AnthropicCollector

    collector = AnthropicCollector(account_id="default", account_label="DEFAULT")
    with patch(
        "app.services.collectors.anthropic_web.token_cache.update_account_metadata",
        AsyncMock(),
    ):
        collector._parse_web_api_response(
            {"five_hour": {"utilization": 10}},
            org_data={"membership": {"user": {"email": "member@example.com"}}},
        )
    assert collector.account_label == "member@example.com"


@pytest.mark.asyncio
async def test_console_card_fills_in_a_placeholder_name(claude_db):
    """The Console path treats every spelling of the placeholder as unset."""
    from app.services.collectors.anthropic_console import AnthropicConsoleCollector
    from app.services.token_cache import token_cache

    source_id = await seed_claude_source(claude_db, "anthropic_console")

    def upstream(request):
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200, json=[{"uuid": ORG_A, "name": "API org", "capabilities": ["api"]}]
            )
        return httpx.Response(200, json={"amount": 1234, "currency": "USD"})

    collector = AnthropicConsoleCollector(account_id="default", account_label="default")
    async with token_cache.using_source("anthropic_console", source_id, source_id):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            await collector._primary_strategy(client)
    assert collector.account_label == "API org"


def test_a_name_never_carries_the_card_details_marker():
    """A name a human reads must be a name, not the card detail's marker."""
    from app.services.claude_verification import (
        claude_account_label,
        claude_identity_string,
    )

    org_data = {"name": "Acme", "membership": {}}
    assert claude_account_label("anthropic", org_data) == "Acme"
    assert claude_account_label("anthropic_console", org_data) == "Acme"
    # The card detail keeps the marker, so the tagger can tell it apart.
    assert claude_identity_string(org_data) == "org: Acme"


def test_the_verified_name_and_billing_model_reach_the_promotion():
    """What the collector proved must be what the promotion writes."""

    class _Proved:
        verified_source_generation = "gen-1"
        verified_organization_id = "org-1"
        verified_account_label = "API org"
        verified_billing_type = "pay_as_you_go"

    from app.services.collector_manager import CollectorManager

    kwargs = CollectorManager._source_proof_kwargs(_Proved())
    assert kwargs["organization_id"] == "org-1"
    assert kwargs["account_label"] == "API org"
    assert kwargs["billing_type"] == "pay_as_you_go"


def test_a_card_without_a_name_keeps_its_own_account():
    """An email-shaped name would file the card under a different account."""
    from app.services.collectors.anthropic import AnthropicCollector

    collector = AnthropicCollector(account_id="person@example.com", account_label="ops@acme.com")
    tagged = collector._tag_results(
        [
            {
                "service_name": "Claude",
                "variant": "5-Hour Limit",
                "window_type": "session",
                "remaining": "80.0%",
            }
        ]
    )
    # The card's label can be what keys it to its account, so it may not be a
    # name that points somewhere else.
    assert tagged[0]["account_label"] == "person@example.com"


@pytest.mark.asyncio
async def test_a_claude_name_never_carries_the_organization_marker():
    """The marker belongs in the card detail, not in a name."""
    from unittest.mock import AsyncMock, patch

    from app.services.collectors.anthropic import AnthropicCollector

    collector = AnthropicCollector(account_id="default")
    with patch(
        "app.services.collectors.anthropic_web.token_cache.update_account_metadata",
        AsyncMock(),
    ):
        result = collector._parse_web_api_response(
            {"five_hour": {"utilization": 10}},
            org_data={"name": "Acme"},
        )
    assert collector.account_label == "Acme"
    assert all(card.get("account_label") == "Acme" for card in result)
    # The detail still carries the marker, so the tagger can read it back.
    assert any("org: Acme" in (card.get("detail") or "") for card in result)
