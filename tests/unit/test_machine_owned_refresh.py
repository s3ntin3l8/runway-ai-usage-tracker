"""The server must not refresh a rotating provider's login that a machine's CLI owns (#445).

Refreshing rotates the refresh token, which signs that CLI out, and the new token never
goes back to it. The sidecar's machine renews its own login and re-pushes it.
"""

from __future__ import annotations

import base64
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import auth_failures
from app.services.collector_manager import CollectorManager
from app.services.token_auto_refresher import TokenAutoRefresher
from app.services.token_cache import TokenCache, server_may_refresh
from app.services.token_refresher import (
    ROTATING_REFRESH_PROVIDERS,
    machine_owns_credential,
)

ALICE = "alice@example.com"


def _jwt(payload: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _bundle(sidecar_id: str | None = "dev-01", **tokens: str) -> dict:
    return {
        "source_id": f"sidecar:{sidecar_id}:x",
        "sidecar_id": sidecar_id,
        "credential_origin": "path:/x.json" if sidecar_id else None,
        "tokens": tokens,
    }


# --- the predicate -------------------------------------------------------------------


@pytest.mark.parametrize("provider", sorted(ROTATING_REFRESH_PROVIDERS))
def test_a_machines_login_is_owned_for_every_rotating_provider(provider):
    key = "xai_refresh" if provider == "xai" else "refresh_token"
    tokens = {key: "rt-1"}
    assert machine_owns_credential(provider, tokens, [_bundle(**tokens)])


def test_gemini_does_not_rotate_so_the_server_may_refresh_it():
    tokens = {"refresh_token": "rt-1"}
    assert not machine_owns_credential(
        "gemini", tokens, [_bundle(**tokens)], merged_source="dev-01"
    )
    assert "gemini" not in ROTATING_REFRESH_PROVIDERS


def test_ownership_follows_the_shared_refresh_secret_not_the_merged_source_label():
    mine = {"refresh_token": "server-rt"}
    theirs = _bundle(refresh_token="machine-rt")
    # A different login for the same account on a machine does not make mine theirs.
    assert not machine_owns_credential("anthropic", mine, [theirs], merged_source="server")
    # But the same secret does, whatever the merged entry says.
    assert machine_owns_credential(
        "anthropic", {"refresh_token": "machine-rt"}, [theirs], merged_source="server"
    )


def test_a_sidecar_as_the_last_pusher_counts_even_without_a_live_bundle():
    assert machine_owns_credential("anthropic", {"refresh_token": "r"}, [], merged_source="dev-01")


@pytest.mark.parametrize("source", [None, "server", "config", "manual_config"])
def test_server_and_config_credentials_are_the_servers_to_refresh(source):
    assert not machine_owns_credential(
        "anthropic",
        {"refresh_token": "r"},
        [_bundle(None, refresh_token="r")],
        merged_source=source,
    )


def test_a_legacy_sidecar_push_without_an_origin_is_still_a_machines():
    tokens = {"refresh_token": "rt"}
    assert machine_owns_credential(
        "anthropic", tokens, [_bundle("old-sidecar", **tokens) | {"credential_origin": None}]
    )


def test_no_refresh_secret_means_nothing_to_own():
    assert not machine_owns_credential(
        "anthropic", {"oauth_token": "a"}, [_bundle(oauth_token="a")]
    )


@pytest.mark.asyncio
async def test_server_may_refresh_reads_the_live_source_bundles(monkeypatch):
    cache = TokenCache()
    monkeypatch.setattr("app.services.token_cache.token_cache", cache)
    await cache.store(
        "anthropic",
        {"oauth_token": "a", "refresh_token": "rt"},
        account_id=ALICE,
        source_id="sidecar:dev-01:x",
        source="dev-01",
        source_metadata={"sidecar_id": "dev-01", "credential_origin": "path:/x.json"},
    )

    assert not await server_may_refresh("anthropic", ALICE, {"refresh_token": "rt"})
    assert await server_may_refresh("anthropic", ALICE, {"refresh_token": "another"})


# --- auto refresh ---------------------------------------------------------------------


@pytest.fixture
def cache(monkeypatch):
    fresh = TokenCache()
    monkeypatch.setattr("app.services.token_auto_refresher.token_cache", fresh)
    monkeypatch.setattr("app.services.token_cache.token_cache", fresh)
    return fresh


@pytest.fixture
def refresher():
    return TokenAutoRefresher(interval_seconds=300, threshold_seconds=600)


async def _store_sidecar(cache, provider, tokens, account=ALICE):
    await cache.store(
        provider,
        tokens,
        account_id=account,
        source_id=f"sidecar:dev-01:{provider}",
        source="dev-01",
        source_metadata={"sidecar_id": "dev-01", "credential_origin": f"path:/{provider}.json"},
    )


@pytest.mark.parametrize(
    ("provider", "tokens"),
    [
        ("anthropic", {"oauth_token": "a", "refresh_token": "rt", "expiry_date": "{soon_ms}"}),
        (
            "chatgpt",
            {
                "oauth_token": _jwt({"exp": time.time() + 120}),
                "refresh_token": "rt",
            },
        ),
        ("xai", {"xai_access": _jwt({"exp": time.time() + 120}), "xai_refresh": "rt"}),
    ],
)
@pytest.mark.asyncio
async def test_the_auto_refresher_leaves_a_machines_rotating_login_alone(
    cache, refresher, provider, tokens
):
    tokens = {
        k: v.replace("{soon_ms}", str(int((time.time() + 120) * 1000))) for k, v in tokens.items()
    }
    await _store_sidecar(cache, provider, tokens)

    mock_refresh = AsyncMock()
    with patch("app.services.token_auto_refresher.refresh_oauth_token", new=mock_refresh):
        count = await refresher.refresh_due()

    assert count == 0
    mock_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_auto_refresher_still_refreshes_a_machines_gemini_login(cache, refresher):
    await _store_sidecar(
        cache,
        "gemini",
        {
            "oauth_token": "v1",
            "refresh_token": "rt",
            "id_token": _jwt({"exp": time.time() + 120, "email": ALICE}),
        },
    )
    mock_refresh = AsyncMock(
        return_value={
            "oauth_token": "v2",
            "refresh_token": "rt",
            "id_token": _jwt({"exp": time.time() + 3600, "email": ALICE}),
        }
    )
    with patch("app.services.token_auto_refresher.refresh_oauth_token", new=mock_refresh):
        assert await refresher.refresh_due() == 1


@pytest.mark.asyncio
async def test_the_auto_refresher_still_refreshes_the_servers_own_anthropic_login(cache, refresher):
    await cache.store(
        "anthropic",
        {
            "oauth_token": "a",
            "refresh_token": "rt",
            "expiry_date": str(int((time.time() + 120) * 1000)),
        },
        account_id=ALICE,
        source="server",
    )
    mock_refresh = AsyncMock(return_value={"oauth_token": "b", "refresh_token": "rt2"})
    with patch("app.services.token_auto_refresher.refresh_oauth_token", new=mock_refresh):
        assert await refresher.refresh_due() == 1


@pytest.mark.asyncio
async def test_a_refresh_result_is_never_written_into_a_machines_rotating_bundle(cache):
    await _store_sidecar(cache, "anthropic", {"oauth_token": "old", "refresh_token": "rt"})
    await _store_sidecar(
        cache, "gemini", {"oauth_token": "old", "refresh_token": "rt"}, account="g@example.com"
    )

    claude = await cache.apply_refresh_to_sources(
        "anthropic", ALICE, {"refresh_token": "rt"}, {"oauth_token": "new", "refresh_token": "rt2"}
    )
    gemini = await cache.apply_refresh_to_sources(
        "gemini", "g@example.com", {"refresh_token": "rt"}, {"oauth_token": "new"}
    )

    assert claude == 0 and gemini == 1
    bundles = await cache.get_source_candidates("anthropic", ALICE)
    assert bundles[0]["tokens"]["refresh_token"] == "rt"


# --- the Refresh endpoints --------------------------------------------------------------


def _client_with(monkeypatch, cache):
    monkeypatch.setattr("app.api.endpoints.system.token_cache", cache)
    monkeypatch.setattr("app.services.token_cache.token_cache", cache)
    return TestClient(app)


@pytest.mark.asyncio
async def test_refreshing_a_machines_login_is_refused_not_performed(monkeypatch):
    cache = TokenCache()
    await _store_sidecar(cache, "anthropic", {"oauth_token": "a", "refresh_token": "rt"})
    client = _client_with(monkeypatch, cache)
    refresh = AsyncMock()
    monkeypatch.setattr("app.services.token_refresher.refresh_oauth_token", refresh)

    by_source = client.post(
        f"/api/v1/system/credentials/anthropic/{ALICE}/sidecar:dev-01:anthropic/refresh"
    )

    assert "sign that CLI out" in by_source.text
    assert by_source.status_code == 409
    refresh.assert_not_awaited()
    assert (await cache.get("anthropic", ALICE))["refresh_token"] == "rt"


@pytest.mark.asyncio
async def test_refreshing_a_machines_gemini_login_still_works(monkeypatch):
    cache = TokenCache()
    await _store_sidecar(cache, "gemini", {"oauth_token": "a", "refresh_token": "rt"})
    client = _client_with(monkeypatch, cache)
    monkeypatch.setattr(
        "app.services.token_refresher.refresh_oauth_token",
        AsyncMock(return_value={"oauth_token": "b", "refresh_token": "rt"}),
    )

    resp = client.post(f"/api/v1/system/credentials/gemini/{ALICE}/sidecar:dev-01:gemini/refresh")

    assert resp.status_code == 200, resp.text


# --- collection: an idle CLI is waiting, not revoked --------------------------------------


@pytest.fixture
def manager():
    return CollectorManager()


def _expired_bundle(source_id, **extra):
    return {
        "source_id": source_id,
        "sidecar_id": "dev-01",
        "credential_origin": f"path:/{source_id}.json",
        "source_type": "file",
        "tokens": {
            "oauth_token": "stale",
            "refresh_token": "rt",
            "expiry_date": str(int((time.time() - 3600) * 1000)),
        },
        **extra,
    }


async def _run_failover(manager, monkeypatch, candidates, collect):
    collector = MagicMock(
        PROVIDER_ID="anthropic", account_id=ALICE, account_label=None, credential_account_id=ALICE
    )
    smart = MagicMock(collector=collector, last_collection_state="fresh")
    smart.reset = AsyncMock()
    smart.collect = AsyncMock(side_effect=collect)
    manager.smart_collectors[f"anthropic:{ALICE}"] = smart

    async def get_candidates(*_a):
        return candidates

    @asynccontextmanager
    async def using_source(*_a):
        yield {"auth_failed": False}

    monkeypatch.setattr(
        "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
    )
    monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)
    health: dict[str, str] = {}
    result = await manager._collect_with_source_failover(f"anthropic:{ALICE}", MagicMock(), health)
    return smart, result, health


@pytest.mark.asyncio
async def test_an_expired_machine_login_is_skipped_without_calling_the_api(manager, monkeypatch):
    auth_failures.reset()

    async def collect(_client):  # would 401 and flag a healthy login as revoked
        raise AssertionError("the API must not be called with an expired machine-owned token")

    smart, result, health = await _run_failover(
        manager, monkeypatch, [_expired_bundle("sidecar:a")], collect
    )

    smart.collect.assert_not_awaited()
    assert result == []
    assert health == {}, "no failure recorded: the row keeps its last real outcome"
    # Nothing ran, so the collector's previous state must not be reported as this poll's:
    # "complete" would reconcile the account's last good cards away.
    smart._set_collection_state.assert_called_once()
    assert smart._set_collection_state.call_args.args[0] == "skipped"
    assert not auth_failures.flagged_accounts("anthropic")


@pytest.mark.asyncio
async def test_failover_moves_past_an_idle_machine_to_a_fresh_source(manager, monkeypatch):
    fresh = {
        "source_id": "sidecar:b",
        "sidecar_id": "mac",
        "credential_origin": "path:/b.json",
        "source_type": "file",
        "tokens": {"oauth_token": "ok", "refresh_token": "rt2"},
    }
    calls = []

    async def collect(_client):
        calls.append(1)
        return [{"service_name": "Claude", "remaining": "80%"}]

    smart, result, health = await _run_failover(
        manager, monkeypatch, [_expired_bundle("sidecar:a"), fresh], collect
    )

    assert len(calls) == 1
    assert result == [{"service_name": "Claude", "remaining": "80%"}]
    assert health == {"sidecar:b": "healthy"}


@pytest.mark.asyncio
async def test_a_dead_machine_login_with_no_refresh_token_is_still_tried(manager, monkeypatch):
    """Nothing will renew it, so let the real call report it dead."""
    dead = _expired_bundle("sidecar:a")
    dead["tokens"].pop("refresh_token")

    async def collect(_client):
        return [{"error_type": "auth_failed", "remaining": "ERR"}]

    smart, _result, health = await _run_failover(manager, monkeypatch, [dead], collect)

    smart.collect.assert_awaited_once()
    assert health == {"sidecar:a": "auth_failed"}


@pytest.mark.asyncio
async def test_an_expired_config_credential_is_not_treated_as_a_machines(manager, monkeypatch):
    server_owned = _expired_bundle("config:anthropic:x", sidecar_id=None, credential_origin=None)

    async def collect(_client):
        return [{"service_name": "Claude", "remaining": "1%"}]

    smart, _result, _health = await _run_failover(manager, monkeypatch, [server_owned], collect)

    smart.collect.assert_awaited_once()


# --- the xAI collector -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_xai_collector_waits_for_the_machine_instead_of_refreshing(monkeypatch):
    from app.services.collectors.xai import XaiCollector

    cache = TokenCache()
    expired = _jwt({"exp": time.time() - 60})
    await _store_sidecar(cache, "xai", {"xai_access": expired, "xai_refresh": "rt"})
    monkeypatch.setattr("app.services.collectors.xai.token_cache", cache)
    monkeypatch.setattr("app.services.token_cache.token_cache", cache)
    refresh = AsyncMock()
    monkeypatch.setattr("app.services.token_refresher.refresh_oauth_token", refresh)

    collector = XaiCollector(account_id=ALICE)
    cards = await collector.collect(MagicMock())

    refresh.assert_not_awaited()
    assert collector._last_error_reason == "renewal_pending"
    assert cards and cards[0].get("error_type") != "auth_failed"


# --- the Anthropic collector's own refresh ---------------------------------------------


@pytest.fixture
def anthropic_cache(monkeypatch):
    fresh = TokenCache()
    for module in (
        "app.services.collectors.anthropic.token_cache",
        "app.services.collectors.anthropic_oauth.token_cache",
        "app.services.collectors.oauth_base.token_cache",
        "app.services.token_cache.token_cache",
    ):
        monkeypatch.setattr(module, fresh)
    return fresh


@pytest.mark.asyncio
async def test_the_anthropic_collector_does_not_exchange_a_machines_refresh_token(
    anthropic_cache, monkeypatch
):
    from app.services.collectors.anthropic import AnthropicCollector

    await _store_sidecar(
        anthropic_cache,
        "anthropic",
        {"oauth_token": "stale", "refresh_token": "machine-rt"},
    )
    post = AsyncMock()
    monkeypatch.setattr("app.services.collectors.anthropic_oauth.http_request_with_retry", post)
    collector = AnthropicCollector(account_id=ALICE)

    async with anthropic_cache.using_source("anthropic", ALICE, "sidecar:dev-01:anthropic"):
        refreshed = await collector._execute_refresh(MagicMock())

    assert refreshed is None
    post.assert_not_awaited()


def _token_endpoint(monkeypatch, status: int = 200) -> AsyncMock:
    resp = MagicMock(status_code=status, headers={})
    resp.json.return_value = {"access_token": "new", "refresh_token": "rt2", "expires_in": 3600}
    post = AsyncMock(return_value=resp)
    monkeypatch.setattr("app.services.collectors.anthropic_oauth.http_request_with_retry", post)
    return post


@pytest.mark.asyncio
async def test_a_config_refresh_token_is_refreshed_by_the_server(anthropic_cache, monkeypatch):
    """The gate keys on where the refresh token came from: a config (Settings) refresh
    token is the server's to refresh, even with no machine bundle for the account."""
    from app.services.collectors.anthropic import AnthropicCollector

    await anthropic_cache.store(
        "anthropic",
        {"oauth_token": "o", "refresh_token": "config-rt"},
        account_id="bob@example.com",
        source="config",
    )
    post = _token_endpoint(monkeypatch)
    collector = AnthropicCollector(account_id="bob@example.com")

    refreshed = await collector._execute_refresh(MagicMock())

    assert refreshed is not None
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_servers_own_claude_refresh_uses_claude_codes_request_shape(
    anthropic_cache, monkeypatch
):
    """JSON body incl. ``scope``, no ``anthropic-beta``: any other shape is answered with 429
    (issue #577), so a server-held Claude login could never be refreshed."""
    from app.services.collectors.anthropic import AnthropicCollector
    from app.services.token_refresher import ANTHROPIC_OAUTH_SCOPES, ANTHROPIC_REFRESH_USER_AGENT

    await anthropic_cache.store(
        "anthropic",
        {"oauth_token": "o", "refresh_token": "config-rt"},
        account_id="bob@example.com",
        source="config",
    )
    post = _token_endpoint(monkeypatch)

    await AnthropicCollector(account_id="bob@example.com")._execute_refresh(MagicMock())

    kwargs = post.await_args.kwargs
    assert kwargs["json"] == {
        "grant_type": "refresh_token",
        "refresh_token": "config-rt",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "scope": " ".join(ANTHROPIC_OAUTH_SCOPES),
    }
    assert kwargs["headers"] == {
        "User-Agent": ANTHROPIC_REFRESH_USER_AGENT,
        "Content-Type": "application/json",
    }
    assert post.await_args.args[1] == "POST"
    assert post.await_args.args[2] == "https://platform.claude.com/v1/oauth/token"


@pytest.mark.asyncio
async def test_a_server_refreshed_claude_token_gets_its_new_expiry(anthropic_cache, monkeypatch):
    """The access token is opaque, so without ``expiry_date`` the merged entry kept the OLD one
    and a freshly refreshed token still read as expired."""
    import time

    from app.services.collectors.anthropic import AnthropicCollector

    stale = str(int((time.time() - 3600) * 1000))
    await anthropic_cache.store(
        "anthropic",
        {"oauth_token": "o", "refresh_token": "config-rt", "expiry_date": stale},
        account_id="bob@example.com",
        source="config",
    )
    _token_endpoint(monkeypatch)  # answers expires_in=3600

    refreshed = await AnthropicCollector(account_id="bob@example.com")._execute_refresh(MagicMock())

    assert refreshed is not None and int(refreshed["expiry_date"]) > time.time() * 1000
    stored = await anthropic_cache.get_token(
        "anthropic", "expiry_date", account_id="bob@example.com"
    )
    assert stored is not None and int(stored) > time.time() * 1000 + 3_000_000


@pytest.mark.asyncio
async def test_a_refresh_response_without_expires_in_still_stores_the_tokens(
    anthropic_cache, monkeypatch
):
    from app.services.collectors.anthropic import AnthropicCollector

    await anthropic_cache.store(
        "anthropic",
        {"oauth_token": "o", "refresh_token": "config-rt"},
        account_id="bob@example.com",
        source="config",
    )
    resp = MagicMock(status_code=200, headers={})
    resp.json.return_value = {"access_token": "new", "refresh_token": "rt2"}
    monkeypatch.setattr(
        "app.services.collectors.anthropic_oauth.http_request_with_retry",
        AsyncMock(return_value=resp),
    )

    refreshed = await AnthropicCollector(account_id="bob@example.com")._execute_refresh(MagicMock())

    assert refreshed == {"access_token": "new", "refresh_token": "rt2"}
    assert (
        await anthropic_cache.get_token("anthropic", "refresh_token", account_id="bob@example.com")
        == "rt2"
    )


# --- alerts: an idle CLI is quiet for a while, not forever ----------------------------------


def _alert_row(expired_for: timedelta, **extra) -> dict:
    return {
        "provider": "anthropic",
        "account_id": ALICE,
        "status": "expired",
        "token_types": ["oauth_token", "refresh_token"],
        "machine_renewed": True,
        "expires_at": (datetime.now(UTC) - expired_for).isoformat(),
        **extra,
    }


def test_a_recently_lapsed_machine_login_does_not_alert():
    from app.services.credential_alerts import _is_alert_bad

    assert not _is_alert_bad(_alert_row(timedelta(hours=10)), {"anthropic": {ALICE}})


def test_a_machine_login_expired_for_days_alerts():
    from app.services.credential_alerts import _is_alert_bad

    assert _is_alert_bad(_alert_row(timedelta(days=4)), {"anthropic": {ALICE}})


def test_the_grace_period_only_applies_to_machine_renewed_logins():
    from app.services.credential_alerts import _is_alert_bad

    row = _alert_row(timedelta(days=4), machine_renewed=False)
    assert not _is_alert_bad(row, {"anthropic": {ALICE}})


# --- a pasted config bundle can hold a machine's refresh secret ---------------------------


@pytest.mark.asyncio
async def test_a_config_bundle_sharing_a_machines_secret_is_refused_too(monkeypatch):
    """Refreshing it would rotate the machine CLI's token just the same."""
    cache = TokenCache()
    await _store_sidecar(cache, "anthropic", {"oauth_token": "a", "refresh_token": "shared-rt"})
    await cache.store(
        "anthropic",
        {"oauth_token": "a", "refresh_token": "shared-rt"},
        account_id=ALICE,
        source_id="config:anthropic:alice",
        source="config",
        source_metadata={},
    )
    client = _client_with(monkeypatch, cache)
    refresh = AsyncMock()
    monkeypatch.setattr("app.services.token_refresher.refresh_oauth_token", refresh)

    resp = client.post(
        f"/api/v1/system/credentials/anthropic/{ALICE}/config:anthropic:alice/refresh"
    )

    assert resp.status_code == 409
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_failover_skips_an_expired_config_bundle_that_shares_a_machines_secret(
    manager, monkeypatch
):
    auth_failures.reset()
    machine = _expired_bundle("sidecar:a")
    pasted = _expired_bundle("config:anthropic:x", sidecar_id=None, credential_origin=None)

    async def collect(_client):
        raise AssertionError("an expired shared-secret token must not reach the API")

    smart, result, health = await _run_failover(manager, monkeypatch, [machine, pasted], collect)

    smart.collect.assert_not_awaited()
    assert result == [] and health == {}
    assert not auth_failures.flagged_accounts("anthropic")
