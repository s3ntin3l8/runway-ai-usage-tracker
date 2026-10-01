"""Server-side identity verification for credentials a sidecar couldn't identify (#444)."""

from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.identity_lookup import google_userinfo_email  # noqa: E402
from app.services.token_cache import TokenCache  # noqa: E402
from scripts import sidecar  # noqa: E402

BOB = "bob@example.com"
GOOGLE_TOKEN = "ya29.opaque-google-access-token"  # pragma: allowlist secret


def _jwt(payload: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


# --- the lookup -----------------------------------------------------------------------


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_userinfo_returns_the_email_for_the_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"email": BOB, "id": "1"})

    async with _client(handler) as client:
        assert await google_userinfo_email(client, GOOGLE_TOKEN) == BOB
    assert seen["auth"] == f"Bearer {GOOGLE_TOKEN}"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": "invalid_token"}),
        httpx.Response(200, json={"id": "1"}),
        httpx.Response(200, json={"email": "not-an-email"}),
        httpx.Response(200, json={"email": 7}),
    ],
)
@pytest.mark.asyncio
async def test_userinfo_leaves_the_identity_unresolved_on_anything_but_an_email(response):
    async with _client(lambda _r: response) as client:
        assert await google_userinfo_email(client, GOOGLE_TOKEN) is None


@pytest.mark.asyncio
async def test_userinfo_never_raises():
    def boom(_request):
        raise httpx.ConnectError("down")

    async with _client(boom) as client:
        assert await google_userinfo_email(client, GOOGLE_TOKEN) is None


# --- the Gemini collector ---------------------------------------------------------------


@pytest.fixture
def cache(monkeypatch) -> TokenCache:
    fresh = TokenCache()
    for module in (
        "app.services.collectors.gemini_api.token_cache",
        "app.services.collectors.gemini_oauth.token_cache",
        "app.services.collectors.oauth_base.token_cache",
    ):
        monkeypatch.setattr(module, fresh)
    return fresh


async def _store(cache, *, pending: bool) -> None:
    await cache.store(
        "gemini",
        {"oauth_token": GOOGLE_TOKEN, "refresh_token": "rt"},
        account_id="default" if pending else BOB,
        source_id="sidecar:dev-01:g",
        source="dev-01",
        source_metadata={
            "sidecar_id": "dev-01",
            "credential_origin": "path:/g.json",
            "identity_pending": pending,
        },
    )


def _collector(account_id: str):
    from app.services.collectors.gemini import GeminiCollector

    collector = GeminiCollector(account_id=account_id)
    collector.credential_account_id = account_id
    return collector


@pytest.mark.asyncio
async def test_a_pending_gemini_credential_learns_its_account_from_google(cache, monkeypatch):
    await _store(cache, pending=True)
    userinfo = AsyncMock(return_value=BOB)
    monkeypatch.setattr("app.services.collectors.gemini_api.google_userinfo_email", userinfo)
    collector = _collector("default")

    async with cache.using_source("gemini", "default", "sidecar:dev-01:g"):
        await collector._resolve_pending_identity(MagicMock())

    assert collector.account_id == BOB and collector.account_label == BOB
    userinfo.assert_awaited_once()
    assert userinfo.await_args.args[1] == GOOGLE_TOKEN


@pytest.mark.parametrize("strategy", ["_primary_strategy", "_strategy_api_wrap"])
@pytest.mark.asyncio
async def test_both_gemini_strategies_identify_before_collecting(cache, monkeypatch, strategy):
    """Cards are tagged with the collector's account at collection time, so the identity
    has to be known before the quota call, not after."""
    await _store(cache, pending=True)
    monkeypatch.setattr(
        "app.services.collectors.gemini_api.google_userinfo_email", AsyncMock(return_value=BOB)
    )
    collector = _collector("default")
    seen = []

    async def collect_via_api(_client):
        seen.append(collector.account_id)
        return []

    monkeypatch.setattr(collector, "_collect_via_api", collect_via_api)

    async with cache.using_source("gemini", "default", "sidecar:dev-01:g"):
        await getattr(collector, strategy)(MagicMock())

    assert seen == [BOB]


@pytest.mark.asyncio
async def test_an_unresolvable_pending_gemini_credential_stays_pending(cache, monkeypatch):
    await _store(cache, pending=True)
    monkeypatch.setattr(
        "app.services.collectors.gemini_api.google_userinfo_email", AsyncMock(return_value=None)
    )
    collector = _collector("default")

    async with cache.using_source("gemini", "default", "sidecar:dev-01:g"):
        await collector._resolve_pending_identity(MagicMock())

    assert collector.account_id == "default"


@pytest.mark.asyncio
async def test_an_identified_gemini_credential_is_never_looked_up(cache, monkeypatch):
    await _store(cache, pending=False)
    userinfo = AsyncMock(return_value="someone-else@example.com")
    monkeypatch.setattr("app.services.collectors.gemini_api.google_userinfo_email", userinfo)
    collector = _collector(BOB)

    async with cache.using_source("gemini", BOB, "sidecar:dev-01:g"):
        await collector._resolve_pending_identity(MagicMock())

    assert collector.account_id == BOB
    userinfo.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_servers_own_default_gemini_collector_is_not_re_identified(cache, monkeypatch):
    """Only the identity-verification run, pinned to a pending sidecar bundle, may change
    the account: the server host's own `default` collector keeps attributing to `default`."""
    userinfo = AsyncMock(return_value=BOB)
    monkeypatch.setattr("app.services.collectors.gemini_api.google_userinfo_email", userinfo)
    collector = _collector("default")

    await collector._resolve_pending_identity(MagicMock())

    assert collector.account_id == "default"
    userinfo.assert_not_awaited()


# --- the sidecar -------------------------------------------------------------------------


def test_the_sidecar_sends_unidentified_gemini_credentials_for_verification():
    assert "gemini" in sidecar._SERVER_IDENTITY_PROVIDERS


def test_chatgpt_is_not_sent_for_verification_there_is_nothing_to_ask():
    assert "chatgpt" not in sidecar._SERVER_IDENTITY_PROVIDERS


def test_openai_access_tokens_carry_the_email_in_a_profile_claim():
    token = _jwt({"https://api.openai.com/profile": {"email": BOB, "email_verified": True}})
    assert sidecar._decode_id_token_email(token) == BOB
    assert sidecar._decode_id_token_email(_jwt({"email": BOB})) == BOB
    assert sidecar._decode_id_token_email(_jwt({"sub": "x"})) is None
    assert sidecar._decode_id_token_email("not-a-jwt") is None


def _collect(provider: str, path: Path):
    config = copy.deepcopy(sidecar.__REGISTRY__["providers"][provider])
    config["rules"] = [r for r in config["rules"] if r["type"] == "file"][:1]
    config["rules"][0]["paths"] = [str(path)]
    results, blocked = sidecar.GenericCollector.collect_provider(provider, config)
    return [c for c in results if c.get("remaining") == "Token"], blocked


def test_a_codex_login_with_no_id_token_email_is_identified_from_its_access_token(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt({"sub": "x"}),
                    "access_token": _jwt({"https://api.openai.com/profile": {"email": BOB}}),
                    "refresh_token": "rt",
                }
            }
        )
    )

    cards, blocked = _collect("chatgpt", auth)

    assert cards and cards[0]["account_id"] == BOB
    assert not blocked


def test_a_gemini_login_with_no_email_is_sent_pending_so_the_server_can_verify_it(tmp_path):
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(
        json.dumps(
            {"access_token": GOOGLE_TOKEN, "refresh_token": "rt", "expiry_date": 4102444800000}
        )
    )

    cards, blocked = _collect("gemini", creds)

    assert len(cards) == 1
    assert cards[0]["metadata"]["identity_pending"] is True
    assert cards[0]["metadata"]["oauth_token"] == GOOGLE_TOKEN
    assert blocked and blocked[0]["provider_id"] == "gemini"


def test_a_gemini_login_with_an_email_is_not_pending(tmp_path):
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(
        json.dumps(
            {
                "access_token": GOOGLE_TOKEN,
                "refresh_token": "rt",
                "id_token": _jwt({"email": BOB}),
            }
        )
    )

    cards, blocked = _collect("gemini", creds)

    assert cards and cards[0]["account_id"] == BOB
    assert not cards[0]["metadata"].get("identity_pending")
    assert not blocked
