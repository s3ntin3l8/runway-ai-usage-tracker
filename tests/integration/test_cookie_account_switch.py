"""A browser account switch behind a machine-scoped cookie tag is caught (#462).

A cookie origin (``cookie:anthropic/session``) is the same string whichever account the browser
is signed into, so a tag on it keeps applying after a switch and the new account's usage lands
on the old one. When the provider reports a *different* email for the polled cookie, the tag is
dropped and the source goes back to pending so the identity verifier can map it again.
"""

from __future__ import annotations

import sys

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import CredentialSource
from app.services.collector_manager import CollectorManager
from app.services.credential_tags import CredentialTagRepo
from app.services.smart_collector import SmartCollector
from app.services.token_cache import TokenCache
from app.services.token_cache import token_cache as global_cache

ALICE = "alice@example.com"
BOB = "bob@example.com"
SIDECAR = "host-a"
ORIGIN = "cookie:anthropic/session"
SOURCE_ID = f"sidecar:{SIDECAR}:{ORIGIN}"
COOKIE = "sk-ant-sid01-fake-cookie"  # pragma: allowlist secret
USAGE = {"five_hour": {"utilization": 10.0, "resets_at": "2099-01-01T00:00:00Z"}}


@pytest.fixture
def world(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    cache = TokenCache()
    for name, module in list(sys.modules.items()):
        if (
            name.startswith("app.")
            and name != "app.services.token_cache"
            and getattr(module, "token_cache", None) is global_cache
        ):
            monkeypatch.setattr(module, "token_cache", cache)
    monkeypatch.setattr("app.core.db.engine", engine)
    monkeypatch.setattr("sqlmodel.Session", Session)
    return engine, cache


async def _tagged_source(
    world,
    account: str,
    *,
    set_by: str = "operator",
    scope=SIDECAR,
    provider: str = "anthropic",
    origin: str = ORIGIN,
    tokens: dict | None = None,
):
    """A cookie source already mapped to ``account``, as it sits after a tag was set."""
    engine, cache = world
    source_id = f"sidecar:{SIDECAR}:{origin}"
    with Session(engine) as session:
        session.add(
            CredentialSource(
                provider_id=provider,
                account_id=account,
                source_id=source_id,
                source_type="cookie",
                source_label=SIDECAR,
                credential_origin=origin,
                sidecar_id=SIDECAR,
            )
        )
        CredentialTagRepo.set_tag(
            session,
            provider_id=provider,
            credential_origin=origin,
            account_id=account,
            sidecar_id=scope,
            set_by=set_by,
        )
        session.commit()
    await cache.store(
        provider,
        tokens or {"cookie_sessionKey": COOKIE},
        account_id=account,
        source_id=source_id,
        source=SIDECAR,
        source_metadata={
            "source_type": "cookie",
            "sidecar_id": SIDECAR,
            "credential_origin": origin,
            "identity_pending": False,
        },
    )


def _claude(email: str | None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": "org-1", "name": "Org"}])
        if path == "/api/account":
            if email is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"email_address": email})
        if path.endswith("/usage"):
            return httpx.Response(200, json=USAGE)
        return httpx.Response(404)

    return handler


async def _poll(account: str, handler, provider: str = "anthropic") -> list[dict]:
    manager = CollectorManager()
    cls, name, ttl = manager.collector_registry[provider]
    collector = cls(account_id=account)
    collector.credential_account_id = account
    key = f"{provider}:{account}"
    manager.smart_collectors[key] = SmartCollector(
        collector=collector, collector_name=name, ttl=ttl
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await manager._collect_with_source_failover(key, client, {})


def _state(engine, provider: str = "anthropic", origin: str = ORIGIN):
    with Session(engine) as session:
        row = session.exec(
            select(CredentialSource).where(CredentialSource.provider_id == provider)
        ).one()
        tag = CredentialTagRepo.get(
            session, provider_id=provider, credential_origin=origin, sidecar_id=SIDECAR
        )
        return row.account_id, tag


@pytest.mark.asyncio
async def test_a_browser_switching_account_drops_the_tag_and_publishes_nothing(world):
    engine, cache = world
    await _tagged_source(world, ALICE)

    result = await _poll(ALICE, _claude(BOB))

    assert result == [], "the new account's usage must not be filed under the old one"
    account, tag = _state(engine)
    assert tag is None
    assert account == SOURCE_ID, "the durable row is pending again (Anthropic keys it by source id)"
    assert await cache.get_source_candidates("anthropic", ALICE) == []


@pytest.mark.asyncio
async def test_the_same_email_keeps_the_tag_whatever_its_case(world):
    engine, cache = world
    await _tagged_source(world, ALICE)

    result = await _poll(ALICE, _claude("Alice@Example.com"))

    assert result, "an unchanged account keeps publishing"
    account, tag = _state(engine)
    assert account == ALICE
    assert tag is not None and tag.account_id == ALICE


@pytest.mark.asyncio
async def test_no_verified_email_leaves_the_tag_alone(world):
    engine, cache = world
    await _tagged_source(world, ALICE)

    await _poll(ALICE, _claude(None))

    assert _state(engine)[1] is not None


@pytest.mark.asyncio
async def test_a_tag_on_a_non_email_account_is_never_second_guessed(world):
    """Operators map credentials to hash-keyed or label-based accounts; comparing those with
    an email would drop the tag on every poll."""
    engine, cache = world
    await _tagged_source(world, "team-shared-seat")

    await _poll("team-shared-seat", _claude(BOB))

    assert _state(engine)[1] is not None


@pytest.mark.asyncio
async def test_a_deployment_wide_tag_is_left_for_the_operator(world):
    """An all-machines tag applies on other machines too; one machine's switch can't say it is
    wrong everywhere."""
    engine, cache = world
    await _tagged_source(world, ALICE, scope=None)

    await _poll(ALICE, _claude(BOB))

    with Session(engine) as session:
        tags = CredentialTagRepo.get(
            session, provider_id="anthropic", credential_origin=ORIGIN, sidecar_id=None
        )
    assert tags is not None
    assert _state(engine)[0] == ALICE, "the source stays where the operator put it"
    assert [c["source_id"] for c in await cache.get_source_candidates("anthropic", ALICE)] == [
        SOURCE_ID
    ]


@pytest.mark.asyncio
async def test_a_source_that_switched_is_reverified_into_the_new_account(world):
    """The whole loop: the tag drops, the sidecar's next push files the cookie as pending, and
    the identity verifier maps it to the new email."""
    engine, cache = world
    await _tagged_source(world, ALICE)
    await _poll(ALICE, _claude(BOB))

    # The sidecar's next push: no tag any more, so the cookie arrives pending.
    await cache.store(
        "anthropic",
        {"cookie_sessionKey": COOKIE},
        account_id=SOURCE_ID,
        source_id=SOURCE_ID,
        source=SIDECAR,
        source_metadata={
            "source_type": "cookie",
            "sidecar_id": SIDECAR,
            "credential_origin": ORIGIN,
            "identity_pending": True,
        },
    )
    manager = CollectorManager()
    cls, name, ttl = manager.collector_registry["anthropic"]
    verifier = cls(account_id="default")
    verifier.credential_account_id = "default"
    key = "anthropic:default:identity-pending"
    manager.smart_collectors[key] = SmartCollector(collector=verifier, collector_name=name, ttl=ttl)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_claude(BOB))) as client:
        await manager._collect_with_source_failover(key, client, {})

    account, tag = _state(engine)
    assert account == BOB
    assert tag is not None and tag.account_id == BOB and tag.set_by == "identity_verification"


@pytest.mark.asyncio
async def test_a_mere_identity_claim_is_not_revoked(world):
    """A claim is a hint the sidecar made, not something an operator or verification decided."""
    engine, cache = world
    await _tagged_source(world, ALICE, set_by="identity_claim")

    await _poll(ALICE, _claude(BOB))

    assert _state(engine)[1] is not None


@pytest.mark.asyncio
async def test_a_tag_on_an_org_suffixed_id_is_not_email_shaped_so_it_is_left_alone(world):
    engine, cache = world
    await _tagged_source(world, "alice@example.com @ Acme")

    await _poll("alice@example.com @ Acme", _claude(BOB))

    assert _state(engine)[1] is not None


@pytest.mark.asyncio
async def test_only_the_switched_source_is_revoked_and_the_other_still_publishes(world):
    """Two machines map a cookie to the same account; one browser switched. The other source
    keeps working and its data is still published."""
    engine, cache = world
    other_origin = "cookie:anthropic/zz-other-profile"  # sorts after the switched source
    other_source = f"sidecar:{SIDECAR}:{other_origin}"
    await _tagged_source(world, ALICE)
    await _tagged_source(world, ALICE, origin=other_origin, tokens={"cookie_sessionKey": "other"})

    def handler(request: httpx.Request) -> httpx.Response:
        # The first cookie is now Bob's; the second is still Alice's.
        who = BOB if COOKIE in request.headers.get("cookie", "") else ALICE
        return _claude(who)(request)

    result = await _poll(ALICE, handler)

    assert result, "the unchanged source still publishes"
    with Session(engine) as session:
        rows = {r.source_id: r.account_id for r in session.exec(select(CredentialSource)).all()}
    assert rows[SOURCE_ID] == SOURCE_ID
    assert rows[other_source] == ALICE


@pytest.mark.asyncio
async def test_a_switched_sources_proof_is_not_read_as_the_next_sources(world):
    """The collector instance is shared across source attempts. If the first source's verified
    email survived into the second attempt, a second source that reports no email would be
    judged (and revoked) on the first one's proof."""
    engine, cache = world
    other_origin = "cookie:anthropic/zz-other-profile"  # sorts after the switched source
    other_source = f"sidecar:{SIDECAR}:{other_origin}"
    await _tagged_source(world, ALICE)
    await _tagged_source(world, ALICE, origin=other_origin, tokens={"cookie_sessionKey": "other"})

    def handler(request: httpx.Request) -> httpx.Response:
        if COOKIE in request.headers.get("cookie", ""):
            return _claude(BOB)(request)
        return _claude(None)(request)  # the second cookie's account page says nothing

    result = await _poll(ALICE, handler)

    assert result, "the second source is judged on its own response, so it still publishes"
    with Session(engine) as session:
        rows = {r.source_id: r.account_id for r in session.exec(select(CredentialSource)).all()}
    assert rows[SOURCE_ID] == SOURCE_ID and rows[other_source] == ALICE


CHATGPT_ORIGIN = "cookie:chatgpt/session"


@pytest.mark.asyncio
async def test_a_chatgpt_cookie_switching_account_is_caught_the_same_way(world):
    engine, cache = world
    await _tagged_source(
        world,
        ALICE,
        provider="chatgpt",
        origin=CHATGPT_ORIGIN,
        tokens={
            "cookie___Secure-next-auth.session-token": "fake-session"
        },  # pragma: allowlist secret
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/session":
            return httpx.Response(200, json={"accessToken": "bearer"})
        if request.url.path == "/backend-api/wham/usage":
            return httpx.Response(
                200,
                json={
                    "email": BOB,
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 5,
                            "limit_window_seconds": 18000,
                            "reset_after_seconds": 100,
                        }
                    },
                },
            )
        return httpx.Response(404)

    result = await _poll(ALICE, handler, provider="chatgpt")

    assert result == []
    account, tag = _state(engine, "chatgpt", CHATGPT_ORIGIN)
    assert tag is None and account == "default"
