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
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import CredentialSource, PendingCredentialTag
from app.services.collector_manager import CollectorManager
from app.services.collectors import chatgpt
from app.services.credential_tags import CredentialTagRepo
from app.services.smart_collector import SmartCollector
from app.services.token_cache import TokenCache

BOB = "bob@example.com"
OTHER = "server-owner@example.com"
SIDECAR = "host-a"

CLAUDE_COOKIE = "sk-ant-sid01-fake-cookie"  # pragma: allowlist secret
CLAUDE_OAUTH = "sk-ant-oat01-fake-oauth"  # pragma: allowlist secret
CHATGPT_COOKIE = "fake-next-auth-session"  # pragma: allowlist secret
CHATGPT_BEARER = "fake-chatgpt-bearer"  # pragma: allowlist secret
SERVER_BEARER = "server-owner-bearer"  # pragma: allowlist secret


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Isolated DB + a fresh TokenCache wired into every module that imported the global."""
    from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine

    # Real manager workers use concurrent Sessions; sharing one StaticPool
    # connection lets a reader roll back another worker's uncommitted writes.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pending-identity.db'}", connect_args=SQLITE_CONNECT_ARGS
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    cache = TokenCache()
    # Load the native verifier before wiring imports, including on isolated -k runs.
    from app.services import claude_verification  # noqa: F401

    for name, module in list(sys.modules.items()):
        # Never the defining module itself.
        if (
            name.startswith("app.")
            and name != "app.services.token_cache"
            and isinstance(getattr(module, "token_cache", None), TokenCache)
        ):
            monkeypatch.setattr(module, "token_cache", cache)
    monkeypatch.setattr("app.core.db.engine", engine)
    # The shared unit fixtures replace sqlmodel.Session with a mock; this test needs the real one.
    monkeypatch.setattr("sqlmodel.Session", Session)
    yield engine, cache
    engine.dispose()


@pytest.mark.asyncio
async def test_world_workers_cannot_read_or_rollback_another_transaction(world):
    """Manager workers need the independent SQLite transactions used in production."""
    engine, _ = world
    with Session(engine) as writer:
        writer.add(
            PendingCredentialTag(
                sidecar_id=SIDECAR, provider_id="anthropic_console", credential_origin="uncommitted"
            )
        )
        writer.flush()

        def read_in_worker():
            with Session(engine) as reader:
                return reader.exec(select(PendingCredentialTag)).all()

        observed = await asyncio.to_thread(read_in_worker)
        writer.commit()
    with Session(engine) as reader:
        persisted = reader.exec(select(PendingCredentialTag)).all()
    assert (len(observed), len(persisted)) == (0, 1)


@pytest.mark.asyncio
async def test_seed_uses_ingest_pending_slot_for_both_claude_providers(world):
    engine, cache = world
    for provider in ("anthropic", "anthropic_console"):
        source_id = await _seed(world, provider, f"cookie:{provider}/store#aaaaaaaaaaaa", {})
        pending = await cache.get_pending_sources(provider)
        assert pending[0]["account_slot"] == source_id
        with Session(engine) as session:
            row = session.exec(
                select(CredentialSource).where(CredentialSource.provider_id == provider)
            ).one()
            assert row.account_id == source_id


async def _seed(world, provider: str, origin: str, tokens: dict):
    """File a pending sidecar source in the same slot as the ingest endpoint."""
    from app.services.credential_sources import pending_cache_slot

    engine, cache = world
    source_id = f"sidecar:{SIDECAR}:{origin}"
    account_id = pending_cache_slot(provider, source_id)
    with Session(engine) as session:
        session.add(
            CredentialSource(
                provider_id=provider,
                account_id=account_id,
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
        account_id=account_id,
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
            return httpx.Response(
                orgs_status, json=[{"uuid": "11111111-1111-4111-8111-111111111111", "name": "Org"}]
            )
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
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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


@pytest.mark.asyncio
async def test_verified_claude_cookie_does_not_publish_disabled_account(world):
    from app.models.db import ProviderConfig

    engine, _cache = world
    await _seed(
        world, "anthropic", "cookie:anthropic/session", {"cookie_sessionKey": CLAUDE_COOKIE}
    )
    with Session(engine) as session:
        session.add(
            ProviderConfig(provider_id="anthropic", account_id=BOB, enabled=False, archived=True)
        )
        session.commit()
    result = await _verify("anthropic", _claude_web())
    assert result == []
    with Session(engine) as session:
        config = session.exec(select(ProviderConfig)).one()
        assert config.enabled is False
        assert config.archived is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "remove", "reassign"])
async def test_claude_proof_is_revalidated_after_collector_returns(world, change):
    engine, cache = world
    source_id = await _seed(
        world, "anthropic", "cookie:anthropic/session", {"cookie_sessionKey": CLAUDE_COOKIE}
    )
    manager = CollectorManager()
    key = _verifier(manager, "anthropic")
    smart = manager.smart_collectors[key]
    collect = smart.collect

    async def interleaved(client):
        result = await collect(client)
        if change == "replace":
            await cache.store(
                "anthropic",
                {"cookie_sessionKey": "different-session"},
                account_id=source_id,
                source_id=source_id,
                source_metadata={"identity_pending": True},
            )
        elif change == "remove":
            await cache.remove_source("anthropic", source_id, source_id)
            with Session(engine) as session:
                session.delete(
                    session.exec(
                        select(CredentialSource).where(CredentialSource.source_id == source_id)
                    ).one()
                )
                session.commit()
        else:
            with Session(engine) as session:
                source = session.exec(
                    select(CredentialSource).where(CredentialSource.source_id == source_id)
                ).one()
                source.account_id = "operator@example.com"
                CredentialTagRepo.set_tag(
                    session,
                    provider_id="anthropic",
                    credential_origin=source.credential_origin,
                    account_id="operator@example.com",
                    sidecar_id=SIDECAR,
                    set_by="operator",
                )
                session.add(source)
                session.commit()
        return result

    smart.collect = interleaved
    async with httpx.AsyncClient(transport=httpx.MockTransport(_claude_web())) as client:
        result = await manager._collect_with_source_failover(key, client, {})
    assert result == []
    with Session(engine) as session:
        tag = CredentialTagRepo.get(
            session,
            provider_id="anthropic",
            credential_origin="cookie:anthropic/session",
            sidecar_id=SIDECAR,
        )
        assert tag is None or tag.account_id != "bob@example.com"


@pytest.mark.asyncio
async def test_saved_org_change_after_claude_verification_discards_old_org_cards(world):
    from app.models.db import ProviderConfig

    engine, _cache = world
    await _seed(
        world, "anthropic", "cookie:anthropic/session", {"cookie_sessionKey": CLAUDE_COOKIE}
    )
    org_a = "11111111-1111-4111-8111-111111111111"
    org_b = "22222222-2222-4222-8222-222222222222"
    with Session(engine) as session:
        session.add(
            ProviderConfig(provider_id="anthropic", account_id=BOB, claude_organization_id=org_a)
        )
        session.commit()
    manager = CollectorManager()
    key = _verifier(manager, "anthropic")
    smart = manager.smart_collectors[key]
    collect = smart.collect

    async def interleaved(client):
        result = await collect(client)
        with Session(engine) as session:
            config = session.exec(select(ProviderConfig)).one()
            config.claude_organization_id = org_b
            session.add(config)
            session.commit()
        return result

    smart.collect = interleaved
    async with httpx.AsyncClient(transport=httpx.MockTransport(_claude_web())) as client:
        result = await manager._collect_with_source_failover(key, client, {})
    assert result == []


@pytest.mark.asyncio
async def test_reconciliation_does_not_promote_new_pending_generation_using_old_automatic_tag(
    world,
):
    _engine, cache = world
    source_id = await _seed(
        world, "anthropic", "cookie:anthropic/session", {"cookie_sessionKey": CLAUDE_COOKIE}
    )
    await _verify("anthropic", _claude_web())
    await cache.store(
        "anthropic",
        {"cookie_sessionKey": "replacement-cookie"},
        account_id=source_id,
        source_id=source_id,
        source=SIDECAR,
        source_metadata={
            "identity_pending": True,
            "credential_origin": "cookie:anthropic/session",
            "sidecar_id": SIDECAR,
            "source_type": "cookie",
        },
    )
    await CollectorManager().reconcile_token_cache_from_durable_tags()
    pending = await cache.get_pending_sources("anthropic")
    assert len(pending) == 1
    assert pending[0]["tokens"]["cookie_sessionKey"] == "replacement-cookie"


@pytest.mark.asyncio
async def test_claude_pending_sources_survive_real_manager_poller_and_resync(
    world, monkeypatch, caplog
):
    from app.models.db import LatestUsage
    from app.services.poller import BackgroundPoller

    engine, cache = world
    # Verifier worker exceptions are deliberately DEBUG-only before identity is
    # known. Capture them so a lifecycle failure retains the actual cause.
    caplog.set_level("DEBUG", logger="app.services.collector_manager")
    monkeypatch.setattr("app.services.token_cache.token_cache", cache)
    await _seed(
        world,
        "anthropic",
        "cookie:anthropic/chrome/Default/store#aaaaaaaaaaaa",
        {"cookie_sessionKey": CLAUDE_COOKIE},
    )
    await _seed(
        world,
        "anthropic_console",
        "cookie:anthropic_console/chrome/Default/store#bbbbbbbbbbbb",
        {"session_cookie": "console-cookie"},
    )
    manager = CollectorManager()
    manager.collector_registry = {
        p: spec
        for p, spec in manager.collector_registry.items()
        if p in ("anthropic", "anthropic_console")
    }
    org = "11111111-1111-4111-8111-111111111111"

    def upstream(request):
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": BOB})
        if request.url.path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": org, "name": "Org", "capabilities": ["api"]}])
        if request.url.path.endswith("/prepaid/credits"):
            return httpx.Response(200, json={"amount": 1234, "currency": "USD"})
        return httpx.Response(200, json=CLAUDE_USAGE)

    monkeypatch.setattr("app.services.poller.manager", manager)
    monkeypatch.setattr("app.services.poller.engine", engine)
    manager._client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), event_hooks={"response": [cache.observe_response]}
    )
    try:
        await manager._sync_collectors(force=True)
        assert len(manager.smart_collectors) == 4
        poller = BackgroundPoller()
        await poller.poll_now()
        await manager._sync_collectors(force=True)
        assert len(await cache.get_all_active_accounts()) == 2
        assert f"anthropic:{BOB}" in manager.smart_collectors
        assert f"anthropic_console:{org}" in manager.smart_collectors
        await poller.poll_now()
        await manager._sync_collectors(force=True)
        assert len(await cache.get_all_active_accounts()) == 2
        with Session(engine) as session:
            rows = session.exec(select(LatestUsage)).all()
            assert {row.provider_id for row in rows} == {"anthropic", "anthropic_console"}
            for row in rows:
                card = json.loads(row.card_json)
                assert not card.get("error_type")
                assert card["data_source"] == "web"
                assert card["input_source"] == "sidecar"
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "remove", "reassign", "organization"])
async def test_poller_revalidates_claude_proof_after_manager_returns(world, monkeypatch, change):
    from app.models.db import LatestUsage, ProviderConfig
    from app.services.poller import BackgroundPoller

    engine, cache = world
    source_id = await _seed(
        world,
        "anthropic",
        "cookie:anthropic/chrome/Default/store#aaaaaaaaaaaa",
        {"cookie_sessionKey": CLAUDE_COOKIE},
    )
    manager = CollectorManager()
    manager.collector_registry = {"anthropic": manager.collector_registry["anthropic"]}
    manager._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_claude_web()),
        event_hooks={"response": [cache.observe_response]},
    )
    collect_all = manager.collect_all

    async def late_change():
        result = await collect_all()
        # Real semaphore stamping + collect_all flattening preserve the private proof.
        assert result and getattr(result[0], "claude_source_proof", None) is not None
        assert "claude_source_proof" not in json.dumps(result)
        if change == "replace":
            await cache.store(
                "anthropic",
                {"cookie_sessionKey": "replacement-cookie"},
                account_id=BOB,
                source_id=source_id,
            )
        elif change == "remove":
            await cache.remove_source("anthropic", BOB, source_id)
        with Session(engine) as session:
            if change == "remove":
                session.delete(session.exec(select(CredentialSource)).one())
            elif change == "reassign":
                source = session.exec(select(CredentialSource)).one()
                source.account_id = "operator@example.com"
                session.add(source)
            elif change == "organization":
                config = session.exec(select(ProviderConfig)).one()
                config.claude_organization_id = "22222222-2222-4222-8222-222222222222"
                session.add(config)
            session.commit()
        return result

    monkeypatch.setattr(manager, "collect_all", late_change)
    monkeypatch.setattr("app.services.poller.manager", manager)
    monkeypatch.setattr("app.services.poller.engine", engine)
    try:
        await BackgroundPoller().poll_now()
        with Session(engine) as session:
            assert session.exec(select(LatestUsage)).all() == []
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,fallback",
    [
        ("replace", False),
        ("remove", False),
        ("reassign", False),
        ("organization", False),
        ("replace", True),
    ],
)
async def test_manager_rejected_regular_claude_poll_preserves_last_good(
    world, monkeypatch, change, fallback
):
    from app.models.db import LatestUsage, LatestUsageContribution, ProviderConfig
    from app.services.poller import BackgroundPoller

    engine, cache = world
    origin = "cookie:anthropic/chrome/Default/store#aaaaaaaaaaaa"
    source_id = await _seed(world, "anthropic", origin, {"cookie_sessionKey": CLAUDE_COOKIE})
    manager = CollectorManager()
    manager.collector_registry = {"anthropic": manager.collector_registry["anthropic"]}

    def upstream(request):
        if request.url.path.endswith("/usage"):
            return httpx.Response(
                200,
                json={
                    "five_hour": {
                        "utilization": 50
                        if "fallback-cookie" in request.headers.get("cookie", "")
                        else 10
                    }
                },
            )
        return _claude_web()(request)

    manager._client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), event_hooks={"response": [cache.observe_response]}
    )
    monkeypatch.setattr("app.services.poller.manager", manager)
    monkeypatch.setattr("app.services.poller.engine", engine)
    try:
        poller = BackgroundPoller()
        await poller.poll_now()
        await manager._sync_collectors(force=True)
        with Session(engine) as session:
            assert len(session.exec(select(LatestUsageContribution)).all()) == 1
        if fallback:
            fallback_origin = "cookie:anthropic/chrome/Other/store#bbbbbbbbbbbb"
            fallback_id = "sidecar:z-fallback"
            with Session(engine) as session:
                session.add(
                    CredentialSource(
                        provider_id="anthropic",
                        account_id=BOB,
                        source_id=fallback_id,
                        source_type="cookie",
                        source_label=SIDECAR,
                        sidecar_id=SIDECAR,
                        credential_origin=fallback_origin,
                        priority=1,
                    )
                )
                CredentialTagRepo.set_tag(
                    session,
                    provider_id="anthropic",
                    credential_origin=fallback_origin,
                    account_id=BOB,
                    sidecar_id=SIDECAR,
                    set_by="identity_verification",
                )
                session.commit()
            await cache.store(
                "anthropic",
                {"cookie_sessionKey": "fallback-cookie"},
                account_id=BOB,
                source_id=fallback_id,
                source=SIDECAR,
                source_metadata={
                    "source_type": "cookie",
                    "sidecar_id": SIDECAR,
                    "credential_origin": fallback_origin,
                    "identity_pending": False,
                    "priority": 1,
                },
            )
            await manager._sync_collectors(force=True)
        smart = manager.smart_collectors[f"anthropic:{BOB}"]
        collect = smart.collect
        changed = False

        async def interleaved(client):
            nonlocal changed
            result = await collect(client)
            if changed:
                return result
            changed = True
            if change == "replace":
                await cache.store(
                    "anthropic",
                    {"cookie_sessionKey": "replacement-cookie"},
                    account_id=BOB,
                    source_id=source_id,
                )
            elif change == "remove":
                await cache.remove_source("anthropic", BOB, source_id)
            with Session(engine) as session:
                if change in ("remove", "reassign"):
                    source = session.exec(
                        select(CredentialSource).where(CredentialSource.source_id == source_id)
                    ).one()
                    if change == "remove":
                        session.delete(source)
                    else:
                        source.account_id = "operator@example.com"
                        session.add(source)
                elif change == "organization":
                    config = session.exec(select(ProviderConfig)).one()
                    config.claude_organization_id = "22222222-2222-4222-8222-222222222222"
                    session.add(config)
                session.commit()
            return result

        monkeypatch.setattr(smart, "collect", interleaved)
        await poller.poll_now()
        outcome = next(o for o in manager.last_collection_outcomes if o["account_id"] == BOB)
        assert outcome["state"] == ("complete" if fallback else "skipped")
        with Session(engine) as session:
            rows = session.exec(select(LatestUsageContribution)).all()
            assert len(rows) == 1
            assert json.loads(rows[0].card_json)["used_value"] == (50 if fallback else 10)
            latest = session.exec(select(LatestUsage)).one()
            assert json.loads(latest.card_json)["used_value"] == (50 if fallback else 10)
    finally:
        await manager.close()
