from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import CredentialSource
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
    monkeypatch.setattr("app.services.collector_manager.token_cache", cache)
    monkeypatch.setattr(manager, "_record_source_health", lambda *_args: None)
    manager.smart_collectors["openrouter:alice@example.com"] = SmartCollector(
        collector, "OpenRouter", ttl=0
    )
    async with httpx.AsyncClient() as client:
        result = await manager._collect_with_semaphore("openrouter:alice@example.com", client)

    assert result[0]["remaining"] == "healthy"
    assert collector.calls == ["broken", "working"]
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
