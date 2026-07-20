"""Integration tests for /api/v1/system/debug/raw/{provider_id}.

Providers not registered in the CollectorManager return an honest 404 — not a
500 that masks the real cause.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app


class _FakeCollector:
    """Duck-typed collector stand-in for testing _debug_* helpers."""

    STRATEGIES: dict = {}

    def _get_strategy_options(self, s_id: str) -> dict:
        entry = self.STRATEGIES.get(s_id)
        if entry and len(entry) == 3:
            return entry[2]
        return {}

    def _resolve_strategies(self) -> list:
        return []

    async def reset(self):
        pass

    async def is_configured(self) -> bool:
        return False

    async def collect(self, client) -> list:
        return []


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session):
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_debug_raw_unknown_provider_returns_404(client):
    # A completely unknown provider has no server collector — expect 404.
    r = client.get("/api/v1/system/debug/raw/nonexistent_provider_xyz")
    assert r.status_code == 404
    assert "nonexistent_provider_xyz" in r.json()["detail"]


# ── Unit tests for module-level debug helpers ────────────────────────────


def test_debug_strategy_label_found():
    from app.api.endpoints.system import _debug_strategy_label

    strategies_dict = {
        "web": ("Web API (web)", "_method"),
        "oauth": ("OAuth API", "_method", {"enrich": True}),
    }
    assert _debug_strategy_label(strategies_dict, "web") == "Web API (web)"
    assert _debug_strategy_label(strategies_dict, "oauth") == "OAuth API"


def test_debug_strategy_label_not_found():
    from app.api.endpoints.system import _debug_strategy_label

    assert _debug_strategy_label({}, "unknown_strategy") == "unknown_strategy"


def test_debug_split_strategies():
    from app.api.endpoints.system import _debug_split_strategies

    collector = _FakeCollector()
    collector.STRATEGIES = {
        "api": ("API", "_api", {}),
        "web": ("Web", "_web"),
        "enrich": ("Enrich", "_enrich", {"enrich": True}),
    }

    dynamic = [
        (AsyncMock(), "api"),
        (AsyncMock(), "web"),
        (AsyncMock(), "enrich"),
    ]

    primary, enrichment = _debug_split_strategies(collector, dynamic)
    assert len(primary) == 2
    assert len(enrichment) == 1
    assert primary[0][1] == "api"
    assert primary[1][1] == "web"
    assert enrichment[0][1] == "enrich"


def test_debug_split_strategies_all_primary():
    from app.api.endpoints.system import _debug_split_strategies

    collector = _FakeCollector()
    collector.STRATEGIES = {"a": ("A", "_a")}

    dynamic = [(AsyncMock(), "a")]
    primary, enrichment = _debug_split_strategies(collector, dynamic)
    assert len(primary) == 1
    assert len(enrichment) == 0


def test_debug_split_strategies_all_enrichment():
    from app.api.endpoints.system import _debug_split_strategies

    collector = _FakeCollector()
    collector.STRATEGIES = {"e": ("E", "_e", {"enrich": True})}

    dynamic = [(AsyncMock(), "e")]
    primary, enrichment = _debug_split_strategies(collector, dynamic)
    assert len(primary) == 0
    assert len(enrichment) == 1


def test_debug_split_strategies_empty():
    from app.api.endpoints.system import _debug_split_strategies

    collector = _FakeCollector()
    primary, enrichment = _debug_split_strategies(collector, [])
    assert primary == []
    assert enrichment == []


# ── _debug_run_one_strategy tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_debug_run_one_strategy_success():
    from app.api.endpoints.system import _debug_run_one_strategy

    collector = _FakeCollector()
    collector.STRATEGIES = {"web": ("Web API", "_web")}

    async def _mock_strategy(client) -> list[dict]:
        return [
            {"service_name": "Claude", "remaining": "45%", "used_value": 55, "limit_value": 100},
            {"service_name": "Sonnet", "remaining": "60%", "used_value": 40, "limit_value": 100},
        ]

    result = await _debug_run_one_strategy(collector, _mock_strategy, "web", "primary")

    assert result["label"] == "Web API"
    assert result["kind"] == "primary"
    assert result["status"] == "success"
    assert result["cards_returned"] == 2
    assert result["cards_summary"] == [
        {"service_name": "Claude", "remaining": "45%"},
        {"service_name": "Sonnet", "remaining": "60%"},
    ]
    assert result["errors"] == []
    assert isinstance(result["requests"], list)
    assert isinstance(result["responses"], list)


@pytest.mark.asyncio
async def test_debug_run_one_strategy_error_response():
    from app.api.endpoints.system import _debug_run_one_strategy

    collector = _FakeCollector()
    collector.STRATEGIES = {"web": ("Web API", "_web")}

    async def _mock_strategy(client) -> list[dict]:
        return [{"service_name": "Claude", "remaining": "ERR"}]

    result = await _debug_run_one_strategy(collector, _mock_strategy, "web", "primary")
    assert result["status"] == "error"
    assert result["cards_returned"] == 1


@pytest.mark.asyncio
async def test_debug_run_one_strategy_exception():
    from app.api.endpoints.system import _debug_run_one_strategy

    collector = _FakeCollector()
    collector.STRATEGIES = {"web": ("Web API", "_web")}

    async def _mock_strategy(client) -> list[dict]:
        raise ValueError("connection refused")

    result = await _debug_run_one_strategy(collector, _mock_strategy, "web", "primary")
    assert result["status"] == "error"
    assert result["cards_returned"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["type"] == "ValueError"
    assert "connection refused" in result["errors"][0]["message"]


@pytest.mark.asyncio
async def test_debug_run_one_strategy_empty():
    from app.api.endpoints.system import _debug_run_one_strategy

    collector = _FakeCollector()

    async def _mock_strategy(client) -> list[dict]:
        return []

    result = await _debug_run_one_strategy(collector, _mock_strategy, "empty", "primary")
    assert result["status"] == "error"
    assert result["cards_returned"] == 0
    assert result["cards_summary"] == []


@pytest.mark.asyncio
async def test_debug_run_one_strategy_enrichment():
    from app.api.endpoints.system import _debug_run_one_strategy

    collector = _FakeCollector()
    collector.STRATEGIES = {"local": ("Local Logs", "_local")}

    async def _mock_strategy(client) -> list[dict]:
        return [{"service_name": "Claude", "remaining": "45%", "token_usage": {"total": 500}}]

    result = await _debug_run_one_strategy(collector, _mock_strategy, "local", "enrichment")
    assert result["kind"] == "enrichment"


# ── Integration test for endpoint with mock collector ─────────────────────


def test_debug_raw_endpoint_with_strategies(client, monkeypatch):
    """Verify the endpoint returns per-strategy results."""
    from app.services.collector_manager import manager
    from app.services.collectors.base import BaseCollector
    from app.services.smart_collector import SmartCollector

    class _MockCollector(BaseCollector):
        PROVIDER_ID = "test_provider"
        STRATEGIES = {"api": ("API Strategy", "_strategy_api")}

        async def _strategy_api(self, client):
            return [
                {"service_name": "Test", "remaining": "50%", "used_value": 50, "limit_value": 100}
            ]

        async def is_configured(self):
            return True

        async def _primary_strategy(self, client):
            return await self._strategy_api(client)

        def _fallback_strategies(self):
            return []

        async def _error_handler(self):
            return []

    async def _noop_sync():
        pass

    monkeypatch.setattr(manager, "_sync_collectors", _noop_sync)
    smart = SmartCollector(_MockCollector(), "test_provider", ttl=9999)
    manager.smart_collectors["test_provider"] = smart

    try:
        r = client.get("/api/v1/system/debug/raw/test_provider")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert "strategies" in data
        assert data["active_strategy"] == "api"
        assert data["active_strategy_card_count"] == 1
        assert "api" in data["strategies"]
        assert data["strategies"]["api"]["label"] == "API Strategy"
        assert data["strategies"]["api"]["status"] == "success"
    finally:
        manager.smart_collectors.pop("test_provider", None)


def test_debug_raw_endpoint_active_strategy_detection(client, monkeypatch):
    """Verify active_strategy picks the first successful primary."""
    from app.services.collector_manager import manager
    from app.services.collectors.base import BaseCollector
    from app.services.smart_collector import SmartCollector

    class _MultiStrategyCollector(BaseCollector):
        PROVIDER_ID = "multi_test"
        STRATEGIES = {
            "first": ("First Strategy", "_strategy_first"),
            "second": ("Second Strategy", "_strategy_second"),
        }

        async def _strategy_first(self, client):
            return [{"service_name": "ERR", "remaining": "ERR"}]

        async def _strategy_second(self, client):
            return [
                {"service_name": "OK", "remaining": "50%", "used_value": 50, "limit_value": 100}
            ]

        async def is_configured(self):
            return True

        async def _primary_strategy(self, client):
            return []

        def _fallback_strategies(self):
            return []

        async def _error_handler(self):
            return []

    async def _noop_sync():
        pass

    monkeypatch.setattr(manager, "_sync_collectors", _noop_sync)
    smart = SmartCollector(_MultiStrategyCollector(), "multi_test", ttl=9999)
    manager.smart_collectors["multi_test"] = smart

    try:
        r = client.get("/api/v1/system/debug/raw/multi_test")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert data["active_strategy"] == "second"
        assert data["active_strategy_card_count"] == 1
    finally:
        manager.smart_collectors.pop("multi_test", None)


def test_debug_raw_endpoint_legacy_collector(client, monkeypatch):
    """Verify legacy collector (no STRATEGIES) returns empty strategies dict."""
    from app.services.collector_manager import manager
    from app.services.collectors.base import BaseCollector
    from app.services.smart_collector import SmartCollector

    class _LegacyCollector(BaseCollector):
        PROVIDER_ID = "legacy_provider"

        async def is_configured(self):
            return True

        async def _primary_strategy(self, client):
            return [{"service_name": "Old", "remaining": "50%"}]

        def _fallback_strategies(self):
            return []

        async def _error_handler(self):
            return []

    async def _noop_sync():
        pass

    monkeypatch.setattr(manager, "_sync_collectors", _noop_sync)
    smart = SmartCollector(_LegacyCollector(), "legacy_provider", ttl=9999)
    manager.smart_collectors["legacy_provider"] = smart

    try:
        r = client.get("/api/v1/system/debug/raw/legacy_provider")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert "_legacy" in data["strategies"]
        assert data["strategies"]["_legacy"]["label"] == "legacy_provider (legacy)"
        assert data["strategies"]["_legacy"]["status"] == "success"
        assert data["strategies"]["_legacy"]["cards_returned"] == 1
        assert data["active_strategy"] is None
    finally:
        manager.smart_collectors.pop("legacy_provider", None)


def test_debug_raw_endpoint_with_enrichment(client, monkeypatch):
    """Verify enrichment strategies are captured and do not affect active_strategy."""
    from app.services.collector_manager import manager
    from app.services.collectors.base import BaseCollector
    from app.services.smart_collector import SmartCollector

    class _EnrichCollector(BaseCollector):
        PROVIDER_ID = "enrich_provider"
        STRATEGIES = {
            "api": ("API Strategy", "_strategy_api"),
            "local": ("Local Enrichment", "_strategy_local", {"enrich": True}),
        }

        async def _strategy_api(self, client):
            return [
                {"service_name": "Quota", "remaining": "50%", "used_value": 50, "limit_value": 100}
            ]

        async def _strategy_local(self, client):
            return [{"service_name": "Quota", "remaining": "50%", "token_usage": {"total": 500}}]

        async def is_configured(self):
            return True

        async def _primary_strategy(self, client):
            return await self._strategy_api(client)

        def _fallback_strategies(self):
            return []

        async def _error_handler(self):
            return []

    async def _noop_sync():
        pass

    monkeypatch.setattr(manager, "_sync_collectors", _noop_sync)
    smart = SmartCollector(_EnrichCollector(), "enrich_provider", ttl=9999)
    manager.smart_collectors["enrich_provider"] = smart

    try:
        r = client.get("/api/v1/system/debug/raw/enrich_provider")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert "api" in data["strategies"]
        assert "local" in data["strategies"]
        assert data["strategies"]["api"]["kind"] == "primary"
        assert data["strategies"]["local"]["kind"] == "enrichment"
        assert data["active_strategy"] == "api"
    finally:
        manager.smart_collectors.pop("enrich_provider", None)


def test_debug_raw_endpoint_collector_error(client, monkeypatch):
    """Verify the endpoint handles collector errors gracefully."""
    from app.services.collector_manager import manager
    from app.services.collectors.base import BaseCollector
    from app.services.smart_collector import SmartCollector

    class _ErrorCollector(BaseCollector):
        PROVIDER_ID = "error_provider"

        async def is_configured(self):
            raise ValueError("connection refused")

        async def _primary_strategy(self, client):
            return []

        def _fallback_strategies(self):
            return []

        async def _error_handler(self):
            return []

    async def _noop_sync():
        pass

    monkeypatch.setattr(manager, "_sync_collectors", _noop_sync)
    smart = SmartCollector(_ErrorCollector(), "error_provider", ttl=9999)
    manager.smart_collectors["error_provider"] = smart

    try:
        r = client.get("/api/v1/system/debug/raw/error_provider")
        # Should return 500 since is_configured raises
        assert r.status_code == 500
    finally:
        manager.smart_collectors.pop("error_provider", None)


def test_debug_raw_endpoint_legacy_collector_exception(client, monkeypatch):
    """Verify legacy collector exception is captured."""
    from app.services.collector_manager import manager
    from app.services.collectors.base import BaseCollector
    from app.services.smart_collector import SmartCollector

    class _FailingLegacyCollector(BaseCollector):
        PROVIDER_ID = "failing_legacy"

        async def is_configured(self):
            return True

        async def _primary_strategy(self, client):
            return []

        def _fallback_strategies(self):
            return []

        async def _error_handler(self):
            return []

        async def collect(self, client):
            raise ValueError("strategy crashed")

    async def _noop_sync():
        pass

    monkeypatch.setattr(manager, "_sync_collectors", _noop_sync)
    smart = SmartCollector(_FailingLegacyCollector(), "failing_legacy", ttl=9999)
    manager.smart_collectors["failing_legacy"] = smart

    try:
        r = client.get("/api/v1/system/debug/raw/failing_legacy")
        assert r.status_code == 200
        data = r.json()
        assert "_legacy" in data["strategies"]
        assert len(data["strategies"]["_legacy"]["errors"]) == 1
        assert data["strategies"]["_legacy"]["errors"][0]["type"] == "ValueError"
    finally:
        manager.smart_collectors.pop("failing_legacy", None)
