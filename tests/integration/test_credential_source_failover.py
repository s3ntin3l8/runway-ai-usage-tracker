from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import (
    CredentialSource,
    PendingCredentialTag,
)
from app.services.collector_manager import CollectorManager
from app.services.smart_collector import SmartCollector
from app.services.token_cache import TokenCache


class _CredentialProbeCollector:
    PROVIDER_ID = "openrouter"
    account_id = "alice@example.com"
    credential_account_id = account_id

    def __init__(self, cache: TokenCache):
        self.cache = cache
        self.calls: list[str] = []

    async def is_configured(self) -> bool:
        return True

    async def collect(self, _client: httpx.AsyncClient) -> list[dict]:
        value = await self.cache.get_token(self.PROVIDER_ID, "api_key", self.account_id)
        self.calls.append(value or "missing")
        if value == "broken":
            raise RuntimeError("temporary collection failure")
        if value == "rejected":
            await self.cache.observe_response(SimpleNamespace(status_code=401))
            return [{"remaining": "ERR", "error_type": "auth_failed"}]
        if value == "partial":
            await self.cache.observe_response(SimpleNamespace(status_code=401))
            return [{"remaining": "healthy", "data_source": "api"}]
        return [{"remaining": "healthy", "data_source": "api"}]

    async def reset(self) -> None:
        return None


@pytest.mark.asyncio
async def test_collector_retries_next_enabled_source_after_401(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                CredentialSource(
                    provider_id="openrouter",
                    account_id="alice@example.com",
                    source_id="first",
                    source_type="sidecar",
                    source_label="first host",
                    sidecar_id="host-a",
                    enabled=True,
                    priority=0,
                    last_seen=datetime.now(UTC),
                ),
                CredentialSource(
                    provider_id="openrouter",
                    account_id="alice@example.com",
                    source_id="disabled",
                    source_type="sidecar",
                    source_label="disabled host",
                    sidecar_id="host-b",
                    enabled=False,
                    priority=1,
                    last_seen=datetime.now(UTC),
                ),
                CredentialSource(
                    provider_id="openrouter",
                    account_id="alice@example.com",
                    source_id="last",
                    source_type="env",
                    source_label="environment",
                    enabled=True,
                    priority=2,
                    last_seen=datetime.now(UTC),
                ),
            ]
        )
        session.commit()

    import app.core.db as core_db

    monkeypatch.setattr(core_db, "engine", engine)
    with Session(engine) as session:
        assert len(session.exec(select(CredentialSource)).all()) == 3
    cache = TokenCache()
    await cache.store(
        "openrouter",
        {"api_key": "rejected"},  # pragma: allowlist secret — fake value for failover test
        account_id="alice@example.com",
        source_id="first",
        source_metadata={"priority": 0, "enabled": True},
    )
    await cache.store(
        "openrouter",
        {"api_key": "disabled"},  # pragma: allowlist secret — fake value for failover test
        account_id="alice@example.com",
        source_id="disabled",
        source_metadata={"priority": 1, "enabled": False},
    )
    await cache.store(
        "openrouter",
        {"api_key": "working"},  # pragma: allowlist secret — fake value for failover test
        account_id="alice@example.com",
        source_id="last",
        source_metadata={"priority": 2, "enabled": True},
    )

    collector = _CredentialProbeCollector(cache)
    manager = CollectorManager()
    monkeypatch.setattr("app.services.collector_manager.token_cache", cache)
    smart = SmartCollector(collector, "OpenRouter", ttl=0)
    original_reset = smart.reset

    async def delayed_reset() -> None:
        await asyncio.sleep(0.01)
        await original_reset()

    reset = AsyncMock(side_effect=delayed_reset)
    smart.reset = reset
    manager.smart_collectors["openrouter:alice@example.com"] = smart
    async with httpx.AsyncClient() as client:
        result = await manager._collect_with_semaphore("openrouter:alice@example.com", client)

    assert result[0]["remaining"] == "healthy"
    assert collector.calls == ["rejected", "working"]
    assert reset.await_count == 2
    await manager.close()


@pytest.mark.asyncio
async def test_collector_returns_empty_when_all_sources_fail_auth(monkeypatch):
    cache = TokenCache()
    await cache.store(
        "openrouter",
        {"api_key": "rejected"},  # pragma: allowlist secret — fake rejected credential
        account_id="alice@example.com",
        source_id="only-source",
        source_metadata={"enabled": True, "priority": 0},
    )
    collector = _CredentialProbeCollector(cache)
    manager = CollectorManager()
    monkeypatch.setattr("app.services.collector_manager.token_cache", cache)
    monkeypatch.setattr(manager, "_record_source_health", lambda *_args: None)
    manager.smart_collectors["openrouter:alice@example.com"] = SmartCollector(
        collector, "OpenRouter", ttl=0
    )
    async with httpx.AsyncClient() as client:
        result = await manager._collect_with_semaphore("openrouter:alice@example.com", client)

    assert result == []
    assert collector.calls == ["rejected"]
    await manager.close()


@pytest.mark.asyncio
async def test_usable_result_survives_other_401_in_same_source(monkeypatch):
    cache = TokenCache()
    await cache.store(
        "openrouter",
        {"api_key": "partial"},  # pragma: allowlist secret — fake value for partial-failure test
        account_id="alice@example.com",
        source_id="one-source",
        source_metadata={"enabled": True, "priority": 0},
    )
    collector = _CredentialProbeCollector(cache)
    manager = CollectorManager()
    health_writes: list[dict[str, str]] = []
    monkeypatch.setattr("app.services.collector_manager.token_cache", cache)
    monkeypatch.setattr(
        manager,
        "_record_source_health",
        lambda _provider, _account, updates: health_writes.append(dict(updates)),
    )
    manager.smart_collectors["openrouter:alice@example.com"] = SmartCollector(
        collector, "OpenRouter", ttl=0
    )
    async with httpx.AsyncClient() as client:
        result = await manager._collect_with_semaphore("openrouter:alice@example.com", client)

    assert result == [{"remaining": "healthy", "data_source": "api"}]
    assert health_writes == [{"one-source": "degraded"}]
    await manager.close()


@pytest.mark.asyncio
async def test_collector_continues_after_non_auth_source_error(monkeypatch):
    cache = TokenCache()
    for source_id, value, priority in (("first", "broken", 0), ("last", "working", 1)):
        await cache.store(
            "openrouter",
            {"api_key": value},  # pragma: allowlist secret — fake values for failover test
            account_id="alice@example.com",
            source_id=source_id,
            source_metadata={"enabled": True, "priority": priority},
        )
    collector = _CredentialProbeCollector(cache)
    manager = CollectorManager()
    health_writes: list[tuple[str, str, dict[str, str]]] = []
    available_slots = manager._semaphore._value
    monkeypatch.setattr("app.services.collector_manager.token_cache", cache)

    def record_health(provider_id, account_id, updates):
        assert manager._semaphore._value == available_slots
        health_writes.append((provider_id, account_id, dict(updates)))

    monkeypatch.setattr(
        manager,
        "_record_source_health",
        record_health,
    )
    manager.smart_collectors["openrouter:alice@example.com"] = SmartCollector(
        collector, "OpenRouter", ttl=0
    )
    async with httpx.AsyncClient() as client:
        result = await manager._collect_with_semaphore("openrouter:alice@example.com", client)

    assert result[0]["remaining"] == "healthy"
    assert collector.calls == ["broken", "working"]
    assert health_writes == [
        (
            "openrouter",
            "alice@example.com",
            {"first": "unavailable", "last": "healthy"},
        )
    ]
    await manager.close()


@pytest.mark.asyncio
async def test_collector_returns_empty_when_every_source_is_disabled(monkeypatch):
    cache = TokenCache()
    await cache.store(
        "openrouter",
        {"api_key": "disabled"},  # pragma: allowlist secret — fake disabled credential
        account_id="alice@example.com",
        source_id="disabled-source",
        source_metadata={"enabled": True, "priority": 0},
    )
    collector = _CredentialProbeCollector(cache)
    manager = CollectorManager()
    manager._credential_source_preferences[("openrouter", "alice@example.com")] = {
        "disabled-source": (False, 0)
    }
    monkeypatch.setattr("app.services.collector_manager.token_cache", cache)
    manager.smart_collectors["openrouter:alice@example.com"] = SmartCollector(
        collector, "OpenRouter", ttl=0
    )
    async with httpx.AsyncClient() as client:
        result = await manager._collect_with_semaphore("openrouter:alice@example.com", client)

    assert result == []
    assert collector.calls == []
    await manager.close()


@pytest.mark.asyncio
async def test_verified_sidecar_identity_promotes_only_its_source(monkeypatch):
    from sqlmodel import SQLModel

    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    source_id = "sidecar:host-a:path:/home/user/auth.json"
    origin = "path:/home/user/auth.json"
    # Keep this target newer than the freshly created source on any test day.
    target_seen = datetime.now(UTC) + timedelta(days=1)
    with Session(engine) as session:
        session.add(
            CredentialSource(
                provider_id="antigravity",
                account_id="default",
                source_id=source_id,
                source_type="sidecar",
                source_label="host-a",
                credential_origin=origin,
                sidecar_id="host-a",
                last_seen=datetime(2026, 9, 29, tzinfo=UTC),
            )
        )
        session.add(
            PendingCredentialTag(
                sidecar_id="host-a", provider_id="antigravity", credential_origin=origin
            )
        )
        session.add(
            CredentialSource(
                provider_id="antigravity",
                account_id="s3ntin3l8@gmail.com",
                source_id=source_id,
                source_type="sidecar",
                source_label="host-a",
                credential_origin=origin,
                sidecar_id="host-a",
                last_seen=target_seen,
            )
        )
        session.commit()

    cache = TokenCache()
    await cache.store(
        "antigravity",
        {"oauth_token": "fake-sidecar-token"},  # pragma: allowlist secret
        account_id="default",
        source_id=source_id,
        source_metadata={"identity_pending": True},
    )
    monkeypatch.setattr("app.core.db.engine", engine)
    # The shared unit fixtures replace sqlmodel.Session with an empty mock;
    # this test exercises the persistence boundary against its isolated DB.
    monkeypatch.setattr("sqlmodel.Session", Session)
    monkeypatch.setattr("app.services.collector_manager.token_cache", cache)
    manager = CollectorManager()
    move_source = cache.move_source

    async def heartbeat_before_cache_move(provider, old_account, new_account, source):
        # Model a concurrent heartbeat after the DB tag commit but before the
        # collector moves the in-memory source bundle.
        await cache.store(
            provider,
            {"oauth_token": "new-heartbeat-token"},  # pragma: allowlist secret
            account_id=old_account,
            source_id=source,
            source_metadata={"identity_pending": True},
        )
        return await move_source(provider, old_account, new_account, source)

    monkeypatch.setattr(cache, "move_source", heartbeat_before_cache_move)

    await manager._promote_source_identity(
        "antigravity", "default", source_id, "S3ntin3l8@gmail.com"
    )

    with Session(engine) as session:
        source = session.exec(
            select(CredentialSource).where(CredentialSource.source_id == source_id)
        ).one()
        tag = CredentialTagRepo.get(
            session,
            provider_id="antigravity",
            credential_origin=origin,
            sidecar_id="host-a",
        )
        pending = PendingCredentialTagRepo.get(
            session,
            sidecar_id="host-a",
            provider_id="antigravity",
            credential_origin=origin,
        )

    assert source.account_id == "s3ntin3l8@gmail.com"
    # The concurrent heartbeat may advance last_seen; promotion preserves the later timestamp.
    assert source.last_seen is not None
    assert source.last_seen.replace(tzinfo=UTC) >= target_seen
    assert tag is not None and tag.set_by == "identity_verification"
    assert pending is None
    assert await cache.get_source_candidates("antigravity", "default") == []
    promoted = await cache.get_source_candidates("antigravity", "s3ntin3l8@gmail.com")
    assert [candidate["source_id"] for candidate in promoted] == [source_id]
    assert promoted[0]["identity_pending"] is False
    await cache.reset()


@pytest.mark.asyncio
async def test_startup_reconciliation_routes_cached_source_from_durable_tag(monkeypatch):
    from sqlmodel import SQLModel

    from app.services.credential_tags import CredentialTagRepo

    source_id = "sidecar:host-a:auth-json"
    configured_source_id = "sidecar:host-a:oauth-json"
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        CredentialTagRepo.set_tag(
            session,
            provider_id="antigravity",
            credential_origin="path:/auth.json",
            account_id="alice@example.com",
            sidecar_id="host-a",
        )
        CredentialTagRepo.set_tag(
            session,
            provider_id="antigravity",
            credential_origin="path:/oauth.json",
            account_id="alice@example.com",
            sidecar_id="host-a",
        )
        session.add(
            CredentialSource(
                provider_id="antigravity",
                account_id="alice@example.com",
                source_id=configured_source_id,
                source_type="sidecar",
                source_label="OAuth file",
                enabled=False,
                priority=3,
                sidecar_id="host-a",
            )
        )
        session.commit()

    cache = TokenCache()
    await cache.store(
        "antigravity",
        {"oauth_token": "fake-token"},  # pragma: allowlist secret
        account_id="default",
        source_id=source_id,
        source_metadata={
            "source_type": "sidecar",
            "credential_origin": "path:/auth.json",
            "sidecar_id": "host-a",
            "identity_pending": True,
        },
    )
    await cache.store(
        "antigravity",
        {"oauth_token": "fake-token-two"},  # pragma: allowlist secret
        account_id="default",
        source_id=configured_source_id,
        source_metadata={
            "source_type": "sidecar",
            "credential_origin": "path:/oauth.json",
            "sidecar_id": "host-a",
            "identity_pending": True,
        },
    )
    monkeypatch.setattr("app.core.db.engine", engine)
    monkeypatch.setattr("sqlmodel.Session", Session)
    monkeypatch.setattr("app.services.collector_manager.token_cache", cache)
    manager = CollectorManager()
    manager._credential_source_preferences[("antigravity", "default")] = {
        source_id: (True, 0),
        configured_source_id: (True, 1),
    }
    manager._credential_source_preferences[("antigravity", "alice@example.com")] = {
        "already-configured": (False, 9),
        source_id: (False, 4),
    }

    reconciled = await manager.reconcile_token_cache_from_durable_tags(provider_id="antigravity")

    assert reconciled == 2
    assert await cache.get_source_candidates("antigravity", "default") == []
    target_sources = await cache.get_source_candidates("antigravity", "alice@example.com")
    assert {source["source_id"] for source in target_sources} == {
        source_id,
        configured_source_id,
    }
    assert all(source["identity_pending"] is False for source in target_sources)
    assert manager._credential_source_preferences[("antigravity", "default")] == {}
    assert manager._credential_source_preferences[("antigravity", "alice@example.com")] == {
        "already-configured": (False, 9),
        source_id: (False, 4),
        configured_source_id: (False, 3),
    }
    await cache.reset()
