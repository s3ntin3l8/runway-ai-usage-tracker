"""Both Claude cloud diagnostics contend for exactly one single-flight gate.

The cloud probe and the chat fixture are separate diagnostics, but they share the
cluster's worker gate and its read-only source authority. These tests pin that
sharing: one diagnostic in flight makes the other report "busy" instead of
starting its own work.
"""

import asyncio

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.claude_chat_fixture import ChatFixtureRequest
from app.models.claude_cloud_probe import ClaudeCloudProbeRequest
from app.models.db import CredentialSource, ProviderConfig
from app.services import claude_chat_fixture as fixture
from app.services import claude_cloud_probe as probe
from app.services import claude_cloud_support as support
from app.services.token_cache import TokenCache

ACCOUNT = "holder@example.invalid"
SOURCE = "shared-gate-fixture"
ORG = "11111111-1111-4111-8111-111111111111"
TARGET = "33333333-3333-4333-8333-333333333333"
COOKIE = "synthetic-cookie-only"


def chat_request():
    return ChatFixtureRequest(
        account_id=ACCOUNT,
        source_id=SOURCE,
        organization_id=ORG,
        conversation_id=TARGET,
    )


def probe_request():
    return ClaudeCloudProbeRequest(account_id=ACCOUNT, source_id=SOURCE)


@pytest.fixture
async def environment(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'support.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(support, "Session", Session)
    monkeypatch.setattr(support, "engine", engine)
    monkeypatch.setattr(support, "gate", asyncio.Lock())
    monkeypatch.setattr("app.services.source_controls._ENABLED", False)
    cache = TokenCache()
    monkeypatch.setattr(probe, "token_cache", cache)
    monkeypatch.setattr(fixture, "token_cache", cache)
    with Session(engine) as session:
        session.add(
            CredentialSource(
                provider_id="anthropic",
                account_id=ACCOUNT,
                source_id=SOURCE,
                source_type="config",
                source_label="Synthetic",
            )
        )
        session.add(
            ProviderConfig(provider_id="anthropic", account_id=ACCOUNT, claude_organization_id=ORG)
        )
        session.commit()
    # One cached source serves both diagnostics: the probe needs the Code access
    # token, the chat fixture needs the manual cookie.
    await cache.store(
        "anthropic",
        {"oauth_token": "fixture-access", "cookie_sessionKey": COOKIE},
        account_id=ACCOUNT,
        source_id=SOURCE,
    )
    yield engine, cache
    engine.dispose()


def unreachable():
    def handler(req):
        pytest.fail(f"A diagnostic reached the network while the gate was held: {req.url.path}")

    return handler


async def run_chat(body, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await fixture.inspect_chat_fixture(body, client)


async def run_probe(body, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await probe.probe_cloud_history(body, client)


@pytest.mark.asyncio
async def test_both_diagnostics_share_one_gate(environment):
    """A held gate makes both diagnostics inert: neither may start its own work."""
    async with support.gate:
        chat = await run_chat(chat_request(), unreachable())
        cloud = await run_probe(probe_request(), unreachable())
    assert chat["outcome"] == "busy"
    assert cloud["outcome"] == "busy"
    assert chat["requests"] == 0 and cloud["requests"] == 0


@pytest.mark.asyncio
async def test_a_chat_fixture_in_flight_makes_the_cloud_probe_busy(environment):
    """Only one diagnostic runs at a time: the probe stands down for the chat fixture."""
    dispatched = asyncio.Event()
    release = asyncio.Event()

    async def hold(req):
        dispatched.set()
        await release.wait()
        return httpx.Response(200, json={})

    in_flight = asyncio.create_task(run_chat(chat_request(), hold))
    await asyncio.wait_for(dispatched.wait(), timeout=5)
    cloud = await run_probe(probe_request(), unreachable())
    assert cloud["outcome"] == "busy"
    release.set()
    chat = await asyncio.wait_for(in_flight, timeout=5)
    assert chat["outcome"] != "busy"


@pytest.mark.asyncio
async def test_a_cloud_probe_in_flight_makes_the_chat_fixture_busy(environment):
    """The same gate in the other direction: the chat fixture stands down for the probe."""
    dispatched = asyncio.Event()
    release = asyncio.Event()

    async def hold(req):
        dispatched.set()
        await release.wait()
        return httpx.Response(200, json={})

    in_flight = asyncio.create_task(run_probe(probe_request(), hold))
    await asyncio.wait_for(dispatched.wait(), timeout=5)
    chat = await run_chat(chat_request(), unreachable())
    assert chat["outcome"] == "busy"
    release.set()
    cloud = await asyncio.wait_for(in_flight, timeout=5)
    assert cloud["outcome"] != "busy"


def test_the_gate_is_one_shared_module_attribute():
    """No diagnostic may grow a private lock; the support module owns the only one."""
    assert isinstance(support.gate, asyncio.Lock)
    assert not hasattr(probe, "_gate")
    assert not hasattr(fixture, "_gate")


@pytest.mark.asyncio
async def test_both_diagnostics_read_the_shared_source_authority(environment, monkeypatch):
    """Both diagnostics read the same source authority through the support module."""
    reads = []
    real = support.authority_read

    def counted(*args):
        reads.append(None)
        return real(*args)

    monkeypatch.setattr(support, "authority_read", counted)

    def benign(req):
        return httpx.Response(200, json={})

    cloud = await run_probe(probe_request(), benign)
    assert cloud["outcome"] != "busy" and reads
    before = len(reads)
    chat = await run_chat(chat_request(), benign)
    assert chat["outcome"] != "busy" and len(reads) > before
