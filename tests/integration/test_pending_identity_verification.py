"""The server verifies Anthropic / ChatGPT credentials a sidecar couldn't identify (#460).

Runs the real verifier path (``CollectorManager._collect_with_source_failover`` on an
``identity-pending`` collector) against an isolated DB and a mocked upstream, so the promotion
(DB row, durable tag, cache slot) is exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import CredentialSource, PendingCredentialTag
from app.services.collector_manager import CollectorManager
from app.services.collectors import chatgpt
from app.services.credential_tags import CredentialTagRepo
from app.services.smart_collector import SmartCollector
from app.services.token_cache import TokenCache
from app.services.token_cache import token_cache as global_cache

BOB = "bob@example.com"
OTHER = "server-owner@example.com"
SIDECAR = "host-a"

CLAUDE_COOKIE = "sk-ant-sid01-fake-cookie"  # pragma: allowlist secret
CLAUDE_OAUTH = "sk-ant-oat01-fake-oauth"  # pragma: allowlist secret
CHATGPT_COOKIE = "fake-next-auth-session"  # pragma: allowlist secret
CHATGPT_BEARER = "fake-chatgpt-bearer"  # pragma: allowlist secret
SERVER_BEARER = "server-owner-bearer"  # pragma: allowlist secret


@pytest.fixture
def world(monkeypatch):
    """Isolated DB + a fresh TokenCache wired into every module that imported the global."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    cache = TokenCache()
    for name, module in list(sys.modules.items()):
        # Never the defining module itself.
        if (
            name.startswith("app.")
            and name != "app.services.token_cache"
            and getattr(module, "token_cache", None) is global_cache
        ):
            monkeypatch.setattr(module, "token_cache", cache)
    monkeypatch.setattr("app.core.db.engine", engine)
    # The shared unit fixtures replace sqlmodel.Session with a mock; this test needs the real one.
    monkeypatch.setattr("sqlmodel.Session", Session)
    return engine, cache


async def _seed(world, provider: str, origin: str, tokens: dict):
    """A pending sidecar source, filed the way the ingest endpoint files it (Anthropic keys
    its pending bundles by source id, everything else by ``default``)."""
    engine, cache = world
    source_id = f"sidecar:{SIDECAR}:{origin}"
    with Session(engine) as session:
        session.add(
            CredentialSource(
                provider_id=provider,
                account_id=source_id if provider == "anthropic" else "default",
                source_id=source_id,
                source_type="cookie",
                source_label=SIDECAR,
                credential_origin=origin,
                sidecar_id=SIDECAR,
            )
        )
        session.add(
            PendingCredentialTag(sidecar_id=SIDECAR, provider_id=provider, credential_origin=origin)
        )
        session.commit()
    await cache.store(
        provider,
        tokens,
        account_id=source_id if provider == "anthropic" else "default",
        source_id=source_id,
        source=SIDECAR,
        source_metadata={
            "source_type": "cookie",
            "sidecar_id": SIDECAR,
            "credential_origin": origin,
            "identity_pending": True,
        },
    )
    return source_id


def _verifier(manager: CollectorManager, provider: str):
    cls, name, ttl = manager.collector_registry[provider]
    collector = cls(account_id="default")
    collector.credential_account_id = "default"
    key = f"{provider}:default:identity-pending"
    manager.smart_collectors[key] = SmartCollector(
        collector=collector, collector_name=name, ttl=ttl
    )
    return key


async def _verify(provider: str, handler) -> list[dict]:
    manager = CollectorManager()
    key = _verifier(manager, provider)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await manager._collect_with_source_failover(key, client, {})


def _outcome(engine, provider: str, source_id: str):
    with Session(engine) as session:
        source = session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider, CredentialSource.source_id == source_id
            )
        ).one()
        tag = CredentialTagRepo.get(
            session,
            provider_id=provider,
            credential_origin=source.credential_origin,
            sidecar_id=SIDECAR,
        )
        return source.account_id, tag


CLAUDE_USAGE = {"five_hour": {"utilization": 10.0, "resets_at": "2099-01-01T00:00:00Z"}}


def _claude_web(*, orgs_status: int = 200, email: str = BOB):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/organizations":
            return httpx.Response(orgs_status, json=[{"uuid": "org-1", "name": "Org"}])
        if path == "/api/account":
            return httpx.Response(200, json={"email_address": email})
        if path.endswith("/usage"):
            return httpx.Response(200, json=CLAUDE_USAGE)
        return httpx.Response(404)

    return handler


# --- Anthropic ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pending_claude_cookie_is_verified_and_promoted_to_its_email(world):
    """Anthropic files a pending bundle under its source id, not ``default``: the verifier
    must still find it, and move it from that slot."""
    engine, cache = world
    origin = "cookie:anthropic/session"
    source_id = await _seed(world, "anthropic", origin, {"cookie_sessionKey": CLAUDE_COOKIE})
    assert [s["source_id"] for s in await cache.get_pending_sources("anthropic")] == [source_id]

    await _verify("anthropic", _claude_web())

    account, tag = _outcome(engine, "anthropic", source_id)
    assert account == BOB
    assert tag is not None and tag.set_by == "identity_verification" and tag.sidecar_id == SIDECAR
    promoted = await cache.get_source_candidates("anthropic", BOB)
    assert [c["source_id"] for c in promoted] == [source_id]
    assert promoted[0]["identity_pending"] is False
    assert await cache.get_pending_sources("anthropic") == []


@pytest.mark.asyncio
async def test_a_pinned_claude_cookie_is_not_verified_as_the_servers_own_cli_login(
    world, monkeypatch, tmp_path
):
    """The server host's ``~/.claude`` login belongs to another account: a pinned bundle must
    be identified by its own credential only."""
    engine, cache = world
    cli = tmp_path / ".claude" / ".credentials.json"
    cli.parent.mkdir()
    cli.write_text(
        json.dumps(
            {
                "claudeAiOauth": {"accessToken": "cli-access", "refreshToken": "cli-rt"},
                "oauthAccount": {"emailAddress": OTHER},
            }
        )
    )
    # Even a planted login in the server host's home is never consulted.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway-config"))
    source_id = await _seed(
        world, "anthropic", "cookie:anthropic/session", {"cookie_sessionKey": CLAUDE_COOKIE}
    )

    await _verify("anthropic", _claude_web())

    assert _outcome(engine, "anthropic", source_id)[0] == BOB


def _claude_oauth(seen: list[httpx.Request], *, profile_status: int = 200, email: str = BOB):
    """Usage works; the profile names the holder; the organization names an admin."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/oauth/usage":
            return httpx.Response(200, json=CLAUDE_USAGE)
        if request.url.path == "/api/oauth/profile":
            if profile_status != 200:
                return httpx.Response(profile_status, json={"error": "forbidden"})
            return httpx.Response(200, json={"account": {"email": email, "full_name": "Bob"}})
        if request.url.path == "/v1/organizations/me":
            return httpx.Response(200, json={"name": "Org", "contact_email": "admin@example.com"})
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_a_pending_claude_oauth_token_is_identified_by_its_own_profile(world):
    """The holder's profile names them, not the organization's contact (which may be an admin)."""
    engine, cache = world
    source_id = await _seed(
        world, "anthropic", "env:CLAUDE_CODE_OAUTH_TOKEN", {"oauth_token": CLAUDE_OAUTH}
    )
    seen: list[httpx.Request] = []

    await _verify("anthropic", _claude_oauth(seen))

    account, tag = _outcome(engine, "anthropic", source_id)
    assert account == BOB
    assert tag is not None and tag.set_by == "identity_verification"
    # The admin the organization lists is neither adopted nor even asked for.
    assert not [r for r in seen if r.url.path == "/v1/organizations/me"]
    profile = [r for r in seen if r.url.path == "/api/oauth/profile"]
    assert [r.headers["authorization"] for r in profile] == [f"Bearer {CLAUDE_OAUTH}"]
    assert await cache.get_pending_sources("anthropic") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_a_claude_token_the_profile_refuses_stays_pending_not_tagged_to_the_org_contact(
    world, status
):
    """A token without the profile scope can't be named: it stays for the operator rather
    than being tagged to whoever the organization lists as its contact."""
    engine, cache = world
    source_id = await _seed(
        world, "anthropic", "env:CLAUDE_CODE_OAUTH_TOKEN", {"oauth_token": CLAUDE_OAUTH}
    )

    await _verify("anthropic", _claude_oauth([], profile_status=status))

    assert _outcome(engine, "anthropic", source_id)[0] == source_id
    assert [s["source_id"] for s in await cache.get_pending_sources("anthropic")] == [source_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_id", "pinned_pending"),
    [
        ("alice@example.com", False),  # an identified collector, not pinned
        ("alice@example.com", True),  # an identified collector, even on a pending bundle
        ("default", False),  # the server's own default collector, not pinned
    ],
)
async def test_only_the_verifier_looks_a_claude_token_up(world, account_id, pinned_pending):
    from app.services.collectors.anthropic import AnthropicCollector

    engine, cache = world
    source_id = await _seed(
        world, "anthropic", "env:CLAUDE_CODE_OAUTH_TOKEN", {"oauth_token": CLAUDE_OAUTH}
    )
    seen: list[httpx.Request] = []
    collector = AnthropicCollector(account_id=account_id)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_claude_oauth(seen))) as client:
        if pinned_pending:
            async with cache.using_source("anthropic", source_id, source_id):
                await collector._get_claude_oauth(client, CLAUDE_OAUTH)
        else:
            await collector._get_claude_oauth(client, CLAUDE_OAUTH)

    assert not [r for r in seen if r.url.path == "/api/oauth/profile"]
    assert collector.account_id == account_id


@pytest.mark.asyncio
async def test_an_expired_pending_claude_login_is_not_refreshed_into_the_default_account(
    world, monkeypatch
):
    """Refreshing a pinned pending bundle would store the new token with no source metadata and
    publish an unidentified credential as the shared ``default`` account. A sidecar's bundle is
    machine-owned (#445), so the server never refreshes it: this pins that for pending ones."""
    engine, cache = world
    source_id = await _seed(
        world,
        "anthropic",
        "path:/home/u/.claude/.credentials.json",
        {
            "oauth_token": CLAUDE_OAUTH,
            "refresh_token": "pending-rt",  # pragma: allowlist secret
        },
    )
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "platform.claude.com":
            posts.append(request)
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        return httpx.Response(401)

    await _verify("anthropic", handler)

    assert posts == []
    assert "default" not in cache._cache.get("anthropic", {})
    assert _outcome(engine, "anthropic", source_id)[0] == source_id


# --- ChatGPT -----------------------------------------------------------------------------


CHATGPT_USAGE = {
    "email": BOB,
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {
            "used_percent": 10,
            "limit_window_seconds": 18000,
            "reset_after_seconds": 3600,
        }
    },
}


def _chatgpt(seen: list[httpx.Request], *, email: str | None = BOB):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/auth/session":
            return httpx.Response(200, json={"accessToken": CHATGPT_BEARER})
        if request.url.path == "/backend-api/wham/usage":
            body = dict(CHATGPT_USAGE)
            if email is None:
                body.pop("email")
            else:
                body["email"] = email
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_a_pending_chatgpt_cookie_is_verified_through_the_usage_endpoint(world):
    engine, cache = world
    source_id = await _seed(
        world,
        "chatgpt",
        "cookie:chatgpt/session",
        {"cookie___Secure-next-auth.session-token": CHATGPT_COOKIE},
    )
    seen: list[httpx.Request] = []

    await _verify("chatgpt", _chatgpt(seen))

    account, tag = _outcome(engine, "chatgpt", source_id)
    assert account == BOB
    assert tag is not None and tag.set_by == "identity_verification"
    # The exchanged bearer is never published into the shared compatibility cache.
    assert CHATGPT_BEARER not in json.dumps(
        {k: v[0] for k, v in cache._cache.get("chatgpt", {}).items()}
    )


@pytest.mark.asyncio
async def test_a_pinned_chatgpt_source_never_uses_the_servers_own_login(world, monkeypatch):
    """With a server ``CHATGPT_OAUTH_TOKEN``/``auth.json`` present, the pinned cookie bundle
    must still be identified by its own cookie, not the server owner's token."""
    engine, cache = world
    from app.services.credential_provider import CredentialMap

    # Patch the class, not the shared instance: undoing an instance patch leaves an instance
    # attribute behind that shadows every later class-level patch of this method.
    monkeypatch.setattr(
        "app.services.credential_provider.CredentialProvider.get_chatgpt_data",
        staticmethod(
            lambda: CredentialMap(
                {"access_token": SERVER_BEARER}, sources={"access_token": "server"}
            )
        ),
    )
    source_id = await _seed(
        world,
        "chatgpt",
        "cookie:chatgpt/session",
        {"cookie___Secure-next-auth.session-token": CHATGPT_COOKIE},
    )
    seen: list[httpx.Request] = []

    await _verify("chatgpt", _chatgpt(seen))

    assert _outcome(engine, "chatgpt", source_id)[0] == BOB
    used = {r.headers.get("authorization") for r in seen}
    assert f"Bearer {SERVER_BEARER}" not in used
    assert f"Bearer {CHATGPT_BEARER}" in used


@pytest.mark.asyncio
async def test_a_chatgpt_bearer_is_not_reused_for_the_next_source(world):
    """Two pending cookies: the second must exchange its own cookie, not reuse the first's
    bearer (the collector instance is shared across attempts)."""
    engine, cache = world
    first = await _seed(
        world,
        "chatgpt",
        "cookie:chatgpt/session",
        {"cookie___Secure-next-auth.session-token": CHATGPT_COOKIE},
    )
    second = await _seed(
        world,
        "chatgpt",
        "env:CHATGPT_SESSION_TOKEN",
        {"session_cookie": "second-cookie"},  # pragma: allowlist secret
    )
    seen: list[httpx.Request] = []
    emails = iter([None, "carol@example.com"])  # first source proves nothing

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/auth/session":
            return httpx.Response(200, json={"accessToken": f"bearer-{len(seen)}"})
        email = next(emails)
        body = {k: v for k, v in CHATGPT_USAGE.items() if k != "email"}
        return httpx.Response(200, json={**body, **({"email": email} if email else {})})

    await _verify("chatgpt", handler)

    exchanges = [r for r in seen if r.url.path == "/api/auth/session"]
    assert len(exchanges) == 2, "each source must exchange its own cookie"
    assert CHATGPT_COOKIE in exchanges[0].headers["cookie"]
    assert "second-cookie" in exchanges[1].headers["cookie"]
    usage_auth = [
        r.headers["authorization"] for r in seen if r.url.path == "/backend-api/wham/usage"
    ]
    assert len(set(usage_auth)) == 2, "the second source must not reuse the first one's bearer"
    # The first works but cannot name its account: it stays pending, and the second still got
    # verified instead of being starved.
    assert _outcome(engine, "chatgpt", first)[0] == "default"
    assert _outcome(engine, "chatgpt", second)[0] == "carol@example.com"


@pytest.mark.asyncio
async def test_verification_does_not_overwrite_a_tag_the_operator_set_meanwhile(world):
    """The operator maps a pending source while the verifier is mid-call: theirs wins."""
    engine, cache = world
    origin = "cookie:chatgpt/session"
    source_id = await _seed(
        world, "chatgpt", origin, {"cookie___Secure-next-auth.session-token": CHATGPT_COOKIE}
    )
    with Session(engine) as session:
        CredentialTagRepo.set_tag(
            session,
            provider_id="chatgpt",
            credential_origin=origin,
            account_id="operator-choice@example.com",
            sidecar_id=SIDECAR,
            set_by="operator",
        )
        session.commit()

    await _verify("chatgpt", _chatgpt([]))

    _account, tag = _outcome(engine, "chatgpt", source_id)
    assert tag is not None
    assert (tag.account_id, tag.set_by) == ("operator-choice@example.com", "operator")


@pytest.mark.asyncio
async def test_a_pending_chatgpt_run_does_not_relabel_the_shared_default_slot(world):
    """A configured default account must keep its own label while a sidecar cookie verifies."""
    engine, cache = world
    await cache.store(
        "chatgpt",
        {"oauth_token": "configured-token"},  # pragma: allowlist secret
        account_id="default",
        account_label="Configured Account",
        source="config",
    )
    await _seed(
        world,
        "chatgpt",
        "cookie:chatgpt/session",
        {"cookie___Secure-next-auth.session-token": CHATGPT_COOKIE},
    )

    await _verify("chatgpt", _chatgpt([]))
    await asyncio.sleep(0)  # let any fire-and-forget metadata task run

    assert cache._cache["chatgpt"]["default"][1].get("account_label") == "Configured Account"


@pytest.mark.asyncio
async def test_the_exchanged_bearer_is_never_written_into_a_sidecars_bundle(world):
    """Once a source is identified it is polled by its own collector: the hour-long bearer
    stored into its bundle would shadow the cookie the bundle really holds."""
    engine, cache = world
    source_id = f"sidecar:{SIDECAR}:cookie:chatgpt/session"
    await cache.store(
        "chatgpt",
        {"cookie___Secure-next-auth.session-token": CHATGPT_COOKIE},
        account_id=BOB,
        source_id=source_id,
        source=SIDECAR,
        source_metadata={"sidecar_id": SIDECAR, "identity_pending": False},
    )
    collector = chatgpt.ChatGPTCollector(account_id=BOB)

    async with cache.using_source("chatgpt", BOB, source_id):
        await collector._store_refreshed_bearer(CHATGPT_BEARER, "sidecar")

    bundle = cache._source_cache["chatgpt"][BOB][source_id][0]
    assert "oauth_token" not in bundle


# --- the sidecar ---------------------------------------------------------------------------


def test_a_chatgpt_env_access_token_is_identified_from_its_profile_claim(monkeypatch):
    import base64
    import copy

    from scripts import sidecar

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    token = f"{b64({'alg': 'none'})}.{b64({'https://api.openai.com/profile': {'email': BOB}})}.s"
    monkeypatch.setenv("CHATGPT_OAUTH_TOKEN", token)
    config = copy.deepcopy(sidecar.__REGISTRY__["providers"]["chatgpt"])
    config["rules"] = [r for r in config["rules"] if r.get("variable") == "CHATGPT_OAUTH_TOKEN"]
    results, _ = sidecar.GenericCollector.collect_provider("chatgpt", config)
    cards = [c for c in results if c.get("remaining") == "Token"]

    assert cards and cards[0]["account_id"] == BOB
    assert cards[0]["metadata"].get("identity_pending") is not True


@pytest.mark.asyncio
async def test_a_resolved_claude_holder_is_labelled_with_their_email(world):
    """Resolving the holder must not unpin the call, and the card is labelled with the
    holder, not an org admin."""
    from app.services.collectors.anthropic import AnthropicCollector

    _, cache = world
    source_id = await _seed(
        world, "anthropic", "env:CLAUDE_CODE_OAUTH_TOKEN", {"oauth_token": CLAUDE_OAUTH}
    )
    collector = AnthropicCollector(account_id="default")
    seen: list[httpx.Request] = []

    async with httpx.AsyncClient(transport=httpx.MockTransport(_claude_oauth(seen))) as client:
        async with cache.using_source("anthropic", source_id, source_id):
            await collector._get_claude_oauth(client, CLAUDE_OAUTH)

    assert collector.account_id == BOB and collector.account_label == BOB
