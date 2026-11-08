"""Unit tests for CollectorManager source failover handling and token priority."""

from __future__ import annotations

import base64
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit._collector_manager_helpers import manager_fixture  # noqa: F401 — fixture


class TestCollectorManagerInitialization:
    @pytest.mark.parametrize(
        "first_result",
        [[], [{"error_type": "auth_failed", "remaining": "ERR"}]],
    )
    @pytest.mark.asyncio
    async def test_source_failover_skips_empty_or_error_results(
        self, manager, monkeypatch, first_result
    ):
        collector = SimpleNamespace(
            PROVIDER_ID="chatgpt",
            account_id="default",
            account_label=None,
            credential_account_id="default",
        )
        smart = MagicMock(collector=collector, last_collection_state="failed")
        smart.reset = AsyncMock()
        results = iter([first_result, [{"service_name": "ChatGPT", "remaining": 5}]])

        async def collect(_client):
            result = next(results)
            smart.last_collection_state = "failed" if result == first_result else "fresh"
            if result and not result[0].get("error_type"):
                collector.account_id = "alice@example.com"
            return result

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["chatgpt:default"] = smart
        candidates = [
            {
                "source_id": f"sidecar:host:{idx}",
                "sidecar_id": "host",
                "credential_origin": f"path:/{idx}.json",
                "source_type": "file",
            }
            for idx in (1, 2)
        ]

        async def get_candidates(*_args):
            return candidates

        @asynccontextmanager
        async def using_source(*_args):
            yield {"auth_failed": False}

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)
        promote = AsyncMock()
        monkeypatch.setattr(manager, "_promote_source_identity", promote)

        result = await manager._collect_with_source_failover("chatgpt:default", MagicMock(), {})

        assert result == [{"service_name": "ChatGPT", "remaining": 5}]
        assert smart.collect.await_count == 2
        promote.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_collect_with_source_failover_handles_auth_failed_card(
        self, manager, monkeypatch
    ):
        smart = MagicMock()
        smart.reset = AsyncMock()
        collector = MagicMock()
        collector.PROVIDER_ID = "xai"
        collector.account_id = "alice@example.com"
        collector.credential_account_id = "alice@example.com"
        collector.account_label = "Alice"
        smart.collector = collector

        attempts = 0

        async def collect(_client):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return [
                    {
                        "error_type": "auth_failed",
                        "data_source": "error",
                        "detail": "xAI session expired",
                    }
                ]
            return [{"service_name": "xAI", "remaining": "80%"}]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["xai:alice@example.com"] = smart
        candidates = [
            {
                "source_id": "sidecar:a:path:/auth.json",
                "source_type": "sidecar",
                "credential_origin": "path:/auth.json",
                "identity_pending": False,
                "priority": 0,
            },
            {
                "source_id": "sidecar:b:path:/auth.json",
                "source_type": "sidecar",
                "credential_origin": "path:/auth.json",
                "identity_pending": False,
                "priority": 1,
            },
        ]

        async def get_candidates(*_args):
            return candidates

        @asynccontextmanager
        async def using_source(*_args):
            yield {"auth_failed": False}

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)

        health_updates = {}
        result = await manager._collect_with_source_failover(
            "xai:alice@example.com", MagicMock(), health_updates
        )

        assert attempts == 2
        assert result == [{"service_name": "xAI", "remaining": "80%"}]
        assert health_updates[candidates[0]["source_id"]] == "auth_failed"
        assert health_updates[candidates[1]["source_id"]] == "healthy"

    @pytest.mark.asyncio
    async def test_collect_with_source_failover_handles_empty_result_with_invalid_api_key_reason(
        self, manager, monkeypatch
    ):
        from app.services.collectors.xai import XaiCollector

        collector = XaiCollector(account_id="alice@example.com")
        smart = MagicMock()
        smart.reset = AsyncMock()
        smart.collector = collector

        attempts = 0

        async def collect(_client):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                collector._last_error_reason = "invalid_api_key"
                return await collector._error_handler()
            return [{"service_name": "xAI", "remaining": "80%"}]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["xai:alice@example.com"] = smart
        candidates = [
            {
                "source_id": "sidecar:a:origin",
                "source_type": "sidecar",
                "credential_origin": "origin",
                "identity_pending": False,
                "priority": 0,
            },
            {
                "source_id": "sidecar:b:origin",
                "source_type": "sidecar",
                "credential_origin": "origin",
                "identity_pending": False,
                "priority": 1,
            },
        ]

        async def get_candidates(*_args):
            return candidates

        @asynccontextmanager
        async def using_source(*_args):
            yield {"auth_failed": False}

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)

        health_updates = {}
        result = await manager._collect_with_source_failover(
            "xai:alice@example.com", MagicMock(), health_updates
        )

        assert attempts == 2
        assert result == [{"service_name": "xAI", "remaining": "80%"}]
        assert health_updates[candidates[0]["source_id"]] == "auth_failed"
        assert health_updates[candidates[1]["source_id"]] == "healthy"

    @pytest.mark.asyncio
    async def test_collect_with_source_failover_preserves_partial_failure_from_usable_source(
        self, manager, monkeypatch
    ):
        smart = MagicMock()
        smart.reset = AsyncMock()
        collector = MagicMock()
        collector.PROVIDER_ID = "anthropic"
        collector.account_id = "alice@example.com"
        collector.credential_account_id = "alice@example.com"
        collector.account_label = "Alice"
        smart.collector = collector

        attempts = 0

        async def collect(_client):
            nonlocal attempts
            attempts += 1
            # Source returns a usable quota card alongside a partial failure card
            return [
                {"service_name": "Claude 3.7 Sonnet", "remaining": "80%"},
                {"service_name": "Claude Opus", "remaining": "ERR"},
            ]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["anthropic:alice@example.com"] = smart
        candidates = [
            {
                "source_id": "sidecar:a:origin",
                "source_type": "sidecar",
                "credential_origin": "origin",
                "identity_pending": False,
                "priority": 0,
            },
            {
                "source_id": "sidecar:b:origin",
                "source_type": "sidecar",
                "credential_origin": "origin",
                "identity_pending": False,
                "priority": 1,
            },
        ]

        async def get_candidates(*_args):
            return candidates

        @asynccontextmanager
        async def using_source(*_args):
            yield {"auth_failed": False}

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)

        health_updates = {}
        result = await manager._collect_with_source_failover(
            "anthropic:alice@example.com", MagicMock(), health_updates
        )

        assert attempts == 1
        assert len(result) == 2
        assert result[0]["remaining"] == "80%"
        assert result[1]["remaining"] == "ERR"
        assert health_updates[candidates[0]["source_id"]] == "healthy"
        assert candidates[1]["source_id"] not in health_updates

    @pytest.mark.asyncio
    async def test_collect_with_source_failover_retries_on_unusable_error_card(
        self, manager, monkeypatch
    ):
        smart = MagicMock()
        smart.reset = AsyncMock()
        collector = MagicMock()
        collector.PROVIDER_ID = "anthropic"
        collector.account_id = "alice@example.com"
        collector.credential_account_id = "alice@example.com"
        collector.account_label = "Alice"
        smart.collector = collector

        attempts = 0

        async def collect(_client):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return [{"service_name": "Claude", "remaining": "ERR"}]
            return [{"service_name": "Claude", "remaining": "90%"}]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["anthropic:alice@example.com"] = smart
        candidates = [
            {
                "source_id": "sidecar:a:origin",
                "source_type": "sidecar",
                "credential_origin": "origin",
                "identity_pending": False,
                "priority": 0,
            },
            {
                "source_id": "sidecar:b:origin",
                "source_type": "sidecar",
                "credential_origin": "origin",
                "identity_pending": False,
                "priority": 1,
            },
        ]

        async def get_candidates(*_args):
            return candidates

        @asynccontextmanager
        async def using_source(*_args):
            yield {"auth_failed": False}

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)

        health_updates = {}
        result = await manager._collect_with_source_failover(
            "anthropic:alice@example.com", MagicMock(), health_updates
        )

        assert attempts == 2
        assert len(result) == 1
        assert result[0]["remaining"] == "90%"
        assert health_updates[candidates[0]["source_id"]] == "unavailable"
        assert health_updates[candidates[1]["source_id"]] == "healthy"

    @pytest.mark.asyncio
    async def test_collect_with_source_failover_handles_invalid_api_key_card(
        self, manager, monkeypatch
    ):
        smart = MagicMock()
        smart.reset = AsyncMock()
        collector = MagicMock()
        collector.PROVIDER_ID = "xai"
        collector.account_id = "alice@example.com"
        collector.credential_account_id = "alice@example.com"
        collector.account_label = "Alice"
        smart.collector = collector

        attempts = 0

        async def collect(_client):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return [
                    {
                        "service_name": "xAI",
                        "remaining": "ERR",
                        "data_source": "error",
                        "error_type": "invalid_api_key",
                    }
                ]
            return [{"service_name": "xAI", "remaining": "50%"}]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["xai:alice@example.com"] = smart
        candidates = [
            {
                "source_id": "sidecar:a:origin",
                "source_type": "sidecar",
                "credential_origin": "origin",
                "identity_pending": False,
                "priority": 0,
            },
            {
                "source_id": "sidecar:b:origin",
                "source_type": "sidecar",
                "credential_origin": "origin",
                "identity_pending": False,
                "priority": 1,
            },
        ]

        async def get_candidates(*_args):
            return candidates

        @asynccontextmanager
        async def using_source(*_args):
            yield {"auth_failed": False}

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)

        health_updates = {}
        result = await manager._collect_with_source_failover(
            "xai:alice@example.com", MagicMock(), health_updates
        )

        assert attempts == 2
        assert len(result) == 1
        assert result[0]["remaining"] == "50%"
        assert health_updates[candidates[0]["source_id"]] == "auth_failed"
        assert health_updates[candidates[1]["source_id"]] == "healthy"


def _jwt(payload: dict) -> str:
    """Unsigned JWT whose ``exp`` the shared extractor can still read."""

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


class TestFailoverPrefersLiveAccessTokens:
    """Attempt order must let a live credential answer before a dead one (#474).

    Priority orders *usable* credentials. A bundle whose access token expired
    days ago used to take the first attempt anyway (priority 0) and burn it on
    a refresh rejection before the machine with the live token (priority 2)
    was ever reached.
    """

    @staticmethod
    def _bundle(source_id: str, priority: int, tokens: dict[str, str]) -> dict:
        return {
            "source_id": source_id,
            "source_type": "sidecar",
            "credential_origin": f"path:/{source_id.rsplit(':', 1)[-1]}.json",
            "sidecar_id": "host",
            "priority": priority,
            "tokens": tokens,
        }

    def _smart(self, manager, collect_results):
        collector = SimpleNamespace(
            PROVIDER_ID="xai",
            account_id="alice@example.com",
            account_label="Alice",
            credential_account_id="alice@example.com",
        )
        smart = MagicMock(collector=collector, last_collection_state="fresh")
        smart.reset = AsyncMock()
        results = iter(collect_results)

        async def collect(_client):
            return next(results)

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["xai:alice@example.com"] = smart
        return smart

    @staticmethod
    def _attempt_recorder(monkeypatch, attempted):
        async def get_candidates(*_args):
            return list(attempted["candidates"])

        @asynccontextmanager
        async def using_source(_provider, _slot, source_id, *_args):
            attempted["order"].append(source_id)
            yield {"auth_failed": False}

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)

    @pytest.mark.asyncio
    async def test_expired_priority_zero_bundle_yields_to_a_live_bundle(self, manager, monkeypatch):
        live_id = "sidecar:host:live"
        stale_id = "sidecar:host:stale"
        good = [{"service_name": "xAI", "remaining": "80%"}]
        smart = self._smart(manager, [good])
        state = {
            "candidates": [
                self._bundle(stale_id, 0, {"oauth_token": _jwt({"exp": time.time() - 86_400})}),
                self._bundle(live_id, 2, {"oauth_token": _jwt({"exp": time.time() + 3_600})}),
            ],
            "order": [],
        }
        self._attempt_recorder(monkeypatch, state)

        health: dict[str, str] = {}
        result = await manager._collect_with_source_failover(
            "xai:alice@example.com", MagicMock(), health
        )

        assert state["order"] == [live_id]
        assert smart.collect.await_count == 1
        assert result == good
        assert health == {live_id: "healthy"}

    @pytest.mark.asyncio
    async def test_all_expired_bundles_keep_configured_priority_order(self, manager, monkeypatch):
        first_id = "sidecar:host:first"
        second_id = "sidecar:host:second"
        dead = {"error_type": "auth_failed", "data_source": "error", "detail": "invalid_grant"}
        good = [{"service_name": "xAI", "remaining": "80%"}]
        self._smart(manager, [[dead], good])
        state = {
            "candidates": [
                self._bundle(first_id, 0, {"oauth_token": _jwt({"exp": time.time() - 86_400})}),
                self._bundle(second_id, 2, {"oauth_token": _jwt({"exp": time.time() - 3_600})}),
            ],
            "order": [],
        }
        self._attempt_recorder(monkeypatch, state)

        health: dict[str, str] = {}
        result = await manager._collect_with_source_failover(
            "xai:alice@example.com", MagicMock(), health
        )

        assert state["order"] == [first_id, second_id]
        assert result == good
        assert health == {first_id: "auth_failed", second_id: "healthy"}

    @pytest.mark.asyncio
    async def test_undatable_bundle_is_never_demoted(self, manager, monkeypatch):
        stale_id = "sidecar:host:stale"
        opaque_id = "sidecar:host:opaque"
        good = [{"service_name": "xAI", "remaining": "80%"}]
        smart = self._smart(manager, [good])
        state = {
            "candidates": [
                self._bundle(stale_id, 0, {"oauth_token": _jwt({"exp": time.time() - 86_400})}),
                self._bundle(opaque_id, 9, {"oauth_token": "opaque-not-a-jwt"}),
            ],
            "order": [],
        }
        self._attempt_recorder(monkeypatch, state)

        health: dict[str, str] = {}
        await manager._collect_with_source_failover("xai:alice@example.com", MagicMock(), health)

        assert state["order"] == [opaque_id]
        assert smart.collect.await_count == 1
        assert health == {opaque_id: "healthy"}
