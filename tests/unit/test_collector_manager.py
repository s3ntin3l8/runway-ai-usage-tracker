import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

from app.services.collector_manager import CollectorManager
from app.services.collectors.base import BaseCollector


@pytest.fixture
def manager():
    m = CollectorManager()
    # Reset state
    m._collect_future = None
    return m


class TestCollectorManagerInitialization:
    def test_init_registry_count(self, manager):
        """Test that default registry contains expected providers."""
        # 15 providers, including xAI and DeepSeek.
        assert len(manager.collector_registry) == 15
        assert "anthropic" in manager.collector_registry
        assert "antigravity" in manager.collector_registry
        assert "xai" in manager.collector_registry
        assert "deepseek" in manager.collector_registry
        assert "openai" not in manager.collector_registry  # chatgpt is the key

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("resolved_account", "expected_result"),
        [
            (None, []),
            (
                "s3ntin3l8@gmail.com",
                [
                    {
                        "service_name": "Antigravity",
                        "remaining": 7,
                        "metadata": {"private": "stripped"},
                    }
                ],
            ),
        ],
    )
    async def test_sidecar_source_requires_verified_identity_before_history(
        self, manager, monkeypatch, resolved_account, expected_result
    ):
        collector = SimpleNamespace(
            PROVIDER_ID="antigravity",
            account_id="default",
            account_label="Default",
            credential_account_id="default",
        )
        smart = MagicMock(collector=collector)
        smart.reset = AsyncMock()
        quota = {
            "service_name": "Antigravity",
            "remaining": 7,
            "metadata": {"private": "stripped"},
        }

        async def collect(_client):
            collector.account_id = resolved_account or "default"
            return [quota]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["antigravity:default:identity-pending"] = smart
        candidate = {
            "source_id": "sidecar:host:path:/home/user/auth.json",
            "source_type": "sidecar",
            "credential_origin": "path:/home/user/auth.json",
            "sidecar_id": "host",
            "identity_pending": True,
        }

        async def get_candidates(*_args):
            return [candidate]

        @asynccontextmanager
        async def using_source(*_args):
            yield {"auth_failed": False}

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_pending_sources", get_candidates
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)
        promote = AsyncMock()
        persist_preview = MagicMock()
        monkeypatch.setattr(manager, "_promote_source_identity", promote)
        monkeypatch.setattr(manager, "_persist_identity_pending_preview", persist_preview)
        health = {}

        result = await manager._collect_with_source_failover(
            "antigravity:default:identity-pending", MagicMock(), health
        )

        assert result == expected_result
        assert health[candidate["source_id"]] == "healthy"
        if resolved_account:
            promote.assert_awaited_once_with(
                "antigravity",
                "default",
                candidate["source_id"],
                resolved_account,
                cache_account_id="default",
            )
        else:
            promote.assert_not_awaited()
            persist_preview.assert_called_once_with(
                "antigravity",
                candidate,
                [{"service_name": "Antigravity", "remaining": 7}],
            )

    def test_registered_collectors_explicitly_opt_into_complete_snapshots(self, manager):
        assert BaseCollector.COMPLETE_SNAPSHOT is False
        for provider_id, (collector_class, _name, _ttl) in manager.collector_registry.items():
            assert collector_class.__dict__.get("COMPLETE_SNAPSHOT") is True, provider_id

    @pytest.mark.asyncio
    async def test_manual_xai_bearer_is_stored_only_as_access_token(self, manager):
        row = MagicMock(
            provider_id="xai",
            api_key="xai-test-access",
            session_cookie=None,
            oai_sc_cookie=None,
            account_id="alice@example.com",
        )
        with patch(
            "app.services.collector_manager.token_cache.store", new_callable=AsyncMock
        ) as store:
            await manager._sync_manual_config_to_cache(row)

        assert store.call_args.args[1] == {"xai_access": "xai-test-access"}

    @pytest.mark.asyncio
    async def test_manual_minimax_key_is_mirrored_to_api_key_slot(self, manager):
        row = MagicMock(
            provider_id="minimax",
            api_key="placeholder",  # pragma: allowlist secret
            session_cookie=None,
            oai_sc_cookie=None,
            account_id="default",
        )
        with patch(
            "app.services.collector_manager.token_cache.store", new_callable=AsyncMock
        ) as store:
            await manager._sync_manual_config_to_cache(row)

        assert store.call_args.args[1] == {
            "oauth_token": "placeholder",
            "api_key": "placeholder",  # pragma: allowlist secret
        }

    @pytest.mark.asyncio
    async def test_manual_kimi_key_is_mirrored_to_api_key_slot(self, manager):
        """Issue #343: the reload path must publish a dashboard-pasted kimi_coding
        key under the api_key slot the collector reads, or an account-keyed row
        survives a restart invisible to _resolve_code_bearer."""
        row = MagicMock(
            provider_id="kimi_coding",
            api_key="sk-kimi-test-123",  # pragma: allowlist secret
            session_cookie=None,
            oai_sc_cookie=None,
            account_id="alice@example.com",
        )
        with patch(
            "app.services.collector_manager.token_cache.store", new_callable=AsyncMock
        ) as store:
            await manager._sync_manual_config_to_cache(row)

        assert store.call_args.args[1] == {
            "oauth_token": "sk-kimi-test-123",  # pragma: allowlist secret
            "api_key": "sk-kimi-test-123",  # pragma: allowlist secret
        }
        assert store.call_args.kwargs["account_id"] == "alice@example.com"
        assert store.call_args.kwargs["source"] == "config"
        assert store.call_args.kwargs["source_metadata"]["enabled"] is True
        assert store.call_args.kwargs["source_metadata"]["priority"] == 0

    @pytest.mark.asyncio
    async def test_manual_deepseek_key_is_mirrored_to_api_key_slot(self, manager):
        """A dashboard-pasted DeepSeek key must land in the api_key slot the
        collector reads — oauth_token alone is invisible to DeepSeekCollector."""
        row = MagicMock(
            provider_id="deepseek",
            api_key="sk-deepseek-test",  # pragma: allowlist secret
            session_cookie=None,
            oai_sc_cookie=None,
            account_id="a" * 12,
        )
        with patch(
            "app.services.collector_manager.token_cache.store", new_callable=AsyncMock
        ) as store:
            await manager._sync_manual_config_to_cache(row)

        assert store.call_args.args[1] == {
            "oauth_token": "sk-deepseek-test",  # pragma: allowlist secret
            "api_key": "sk-deepseek-test",  # pragma: allowlist secret
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider_id", ["openrouter", "zai", "kimi_api", "kimi_k2"])
    async def test_manual_api_provider_keys_are_mirrored_for_collectors(self, manager, provider_id):
        row = MagicMock(
            provider_id=provider_id,
            api_key="sk-test-key",  # pragma: allowlist secret
            session_cookie=None,
            oai_sc_cookie=None,
            account_id="alice@example.com",
        )
        with patch(
            "app.services.collector_manager.token_cache.store", new_callable=AsyncMock
        ) as store:
            await manager._sync_manual_config_to_cache(row)

        assert store.call_args.args[1] == {
            "oauth_token": "sk-test-key",  # pragma: allowlist secret
            "api_key": "sk-test-key",  # pragma: allowlist secret
        }
        assert store.call_args.kwargs["account_id"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_sync_collectors_default(self, manager):
        """Test that default collectors are spawned."""
        # Clean state
        manager.smart_collectors = {}

        await manager._sync_collectors()

        # Check that some default collectors are present
        assert "anthropic:default" in manager.smart_collectors
        assert "gemini:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_default_collectors_pin_credential_account_id(self, manager):
        """Auth-failure flagging attributes a rejected default key to `default`,
        not to whatever identity the collector resolved for its cards."""
        manager.smart_collectors = {}

        await manager._sync_collectors()

        for key in ("anthropic:default", "gemini:default", "github:default"):
            if key in manager.smart_collectors:
                assert manager.smart_collectors[key].collector.credential_account_id == "default"

    @pytest.mark.asyncio
    async def test_named_accounts_run_alongside_default_collector(self, manager):
        manager.smart_collectors = {}
        with patch(
            "app.services.collector_manager.token_cache.get_all_active_accounts",
            new_callable=AsyncMock,
            return_value=[("anthropic", "alice@example.com", "Alice")],
        ):
            await manager._sync_collectors(force=True)

        assert "anthropic:default" in manager.smart_collectors
        assert "anthropic:alice@example.com" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_pending_source_gets_a_verifier_even_if_default_collector_exists(self, manager):
        manager.smart_collectors = {
            "antigravity:default": MagicMock(
                collector=SimpleNamespace(
                    PROVIDER_ID="antigravity",
                    account_id="alice@example.com",
                    credential_account_id="default",
                    account_label=None,
                    _user_strategies=None,
                    apply_strategy_config=MagicMock(),
                )
            )
        }

        async def pending_sources(provider_id):
            if provider_id == "antigravity":
                return [
                    {
                        "source_id": "sidecar:laptop:auth-json",
                        "source_type": "sidecar",
                        "sidecar_id": "laptop",
                        "credential_origin": "path:/auth.json",
                        "identity_pending": True,
                        "account_slot": "default",
                    }
                ]
            return []

        with patch(
            "app.services.collector_manager.token_cache.get_pending_sources",
            side_effect=pending_sources,
        ):
            await manager._sync_collectors(force=True)

        verifier = manager.smart_collectors["antigravity:default:identity-pending"]
        assert verifier.collector.account_id == "default"
        assert verifier.collector.credential_account_id == "default"

    @pytest.mark.asyncio
    async def test_verifier_repins_credential_slot_between_pending_sources(
        self, manager, monkeypatch
    ):
        collector = SimpleNamespace(
            PROVIDER_ID="antigravity",
            account_id="default",
            account_label=None,
            credential_account_id="default",
        )
        smart = MagicMock(collector=collector)
        smart.reset = AsyncMock()
        account_ids = iter(["alice@example.com", "bob@example.com"])
        credential_slots = []

        async def collect(_client):
            credential_slots.append(collector.credential_account_id)
            resolved_id = next(account_ids)
            collector.account_id = resolved_id
            collector.credential_account_id = resolved_id
            if len(credential_slots) == 1:
                return [{"error_type": "api_error"}]
            return [{"service_name": resolved_id}]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["antigravity:default:identity-pending"] = smart
        candidates = [
            {
                "source_id": f"sidecar:laptop:{origin}",
                "source_type": "sidecar",
                "sidecar_id": "laptop",
                "credential_origin": origin,
                "identity_pending": True,
            }
            for origin in ("path:/one.json", "path:/two.json")
        ]

        async def get_candidates(*_args):
            return candidates

        @asynccontextmanager
        async def using_source(*_args):
            yield {"auth_failed": False}

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_pending_sources", get_candidates
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", using_source)
        promote = AsyncMock()
        monkeypatch.setattr(manager, "_promote_source_identity", promote)

        result = await manager._collect_with_source_failover(
            "antigravity:default:identity-pending", MagicMock(), {}
        )

        assert credential_slots == ["default", "default"]
        assert result == [{"service_name": "bob@example.com"}]
        promote.assert_awaited_once_with(
            "antigravity",
            "default",
            candidates[1]["source_id"],
            "bob@example.com",
            cache_account_id="default",
        )

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

    @pytest.mark.asyncio
    async def test_default_xai_collector_does_not_borrow_durable_identity(self, manager):
        manager.smart_collectors = {}
        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
                return_value=[("xai", "alice@example.com", "Alice")],
            ),
            patch("sqlmodel.Session") as session_cls,
        ):
            inner = MagicMock()
            inner.exec.return_value.all.side_effect = [
                [],  # ProviderConfig rows
                [],  # CredentialSource rows
                [("xai", "alice@example.com")],  # durable LatestUsage identity
            ]
            inner.exec.return_value.first.return_value = None
            session_cls.return_value.__enter__.return_value = inner

            await manager._sync_collectors(force=True)

        xai_default = manager.smart_collectors["xai:default"].collector
        assert not xai_default.account_id
        assert xai_default.CREDENTIALS_KEYED_BY_ACCOUNT_ID
        assert not hasattr(xai_default, "credential_account_id")
        # Historical identity does not prove that the default credential is
        # owned by Alice. Keep the named credential on its own collector.
        assert "xai:alice@example.com" in manager.smart_collectors
        with patch(
            "app.services.collectors.xai.token_cache.get_token",
            new_callable=AsyncMock,
            return_value="alice-access-token",  # pragma: allowlist secret
        ) as get_token:
            assert await xai_default.is_configured()
        get_token.assert_awaited_once_with("xai", "xai_access", account_id="default")

    @pytest.mark.asyncio
    async def test_durable_identity_does_not_relabel_default_credential_slot(self, manager):
        manager.smart_collectors = {}
        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
                return_value=[("kimi_coding", "alice@example.com", "Alice")],
            ),
            patch("sqlmodel.Session") as session_cls,
        ):
            inner = MagicMock()
            inner.exec.return_value.all.side_effect = [
                [],  # ProviderConfig rows
                [],  # CredentialSource rows
                [("kimi_coding", "alice@example.com")],  # durable LatestUsage identity
            ]
            inner.exec.return_value.first.return_value = None
            session_cls.return_value.__enter__.return_value = inner

            await manager._sync_collectors(force=True)

        default_collector = manager.smart_collectors["kimi_coding:default"].collector
        assert not default_collector.account_id
        assert default_collector.credential_account_id == "default"
        # The default collector reads the `default` credential slot; the named
        # sidecar credentials must stay available to the account-keyed collector.
        assert "kimi_coding:alice@example.com" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_sync_collectors_prunes_stale_dynamic_collectors(self, manager):
        """Test that collectors for missing accounts are removed."""
        # Add a fake dynamic collector
        manager.smart_collectors["anthropic:stale-account"] = MagicMock()

        # Mock token_cache to return no dynamic accounts
        with patch(
            "app.services.collector_manager.token_cache.get_all_active_accounts",
            new_callable=AsyncMock,
        ) as mock_accounts:
            mock_accounts.return_value = []  # No dynamic accounts

            # Reset sync time to bypass throttle
            manager._last_sync_time = 0
            await manager._sync_collectors()

            assert "anthropic:stale-account" not in manager.smart_collectors
            # Defaults should remain
            assert "anthropic:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_sync_collectors_force_bypasses_throttle(self, manager):
        """Config mutations pass force=True and must not wait out the 60s throttle."""
        manager.smart_collectors = {}
        manager._last_sync_time = time.time()  # just synced — throttle would skip

        await manager._sync_collectors()  # non-forced → skipped by throttle
        assert manager.smart_collectors == {}

        await manager._sync_collectors(force=True)
        assert "anthropic:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_no_default_collector_when_only_non_default_rows(self, manager):
        """Config rows exist for email accounts but no default sentinel →
        the blanket default must not spawn (it would shadow step 2's
        per-account enabled checks and keep collecting after a disable)."""
        manager.smart_collectors = {}

        class _Cfg:
            provider_id = "anthropic"
            account_id = "alice@example.com"
            enabled = True
            archived = False
            poll_interval_seconds = None
            account_label = None
            strategies = None
            api_key = None
            session_cookie = None

        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
            ) as mock_accounts,
            patch("sqlmodel.Session") as mock_session_cls,
        ):
            mock_accounts.return_value = []
            inner = MagicMock()
            # Order inside _sync_collectors: ProviderConfig.all(),
            # SystemConfig.first(), LatestUsage.all().
            inner.exec.return_value.all.side_effect = [
                [_Cfg()],  # ProviderConfig rows
                [],  # LatestUsage identities
            ]
            inner.exec.return_value.first.return_value = None  # SystemConfig
            mock_session_cls.return_value.__enter__.return_value = inner

            manager._last_sync_time = 0
            await manager._sync_collectors(force=True)

        assert "anthropic:default" not in manager.smart_collectors
        # Other providers (no config rows) still get their defaults.
        assert "gemini:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_dynamic_account_does_not_borrow_default_row_label(self, manager):
        """An account with no config row of its own may inherit the ``default``
        row's enabled/poll/strategy settings, but never its ``account_label`` —
        that label names a different account and would stamp this one's cards
        with someone else's email."""
        manager.smart_collectors = {}

        class _DefaultCfg:
            provider_id = "github"
            account_id = "default"
            enabled = True
            archived = False
            poll_interval_seconds = 123
            account_label = "alice@example.com"
            strategies = None
            api_key = None
            session_cookie = None

        class _OwnCfg(_DefaultCfg):
            account_id = "carol"
            account_label = "carol@example.com"

        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
            ) as mock_accounts,
            patch("sqlmodel.Session") as mock_session_cls,
        ):
            mock_accounts.return_value = [
                ("github", "bob", None),
                ("github", "carol", None),
            ]
            inner = MagicMock()
            inner.exec.return_value.all.side_effect = [
                [_DefaultCfg(), _OwnCfg()],  # ProviderConfig rows
                [],  # LatestUsage identities
            ]
            inner.exec.return_value.first.return_value = None
            mock_session_cls.return_value.__enter__.return_value = inner

            manager._last_sync_time = 0
            await manager._sync_collectors(force=True)

        bob = manager.smart_collectors["github:bob"]
        carol = manager.smart_collectors["github:carol"]
        assert bob.collector.account_label != "alice@example.com"
        assert bob.ttl == 123  # non-label fallbacks still come from ``default``
        assert carol.collector.account_label == "carol@example.com"

    def test_record_source_health_follows_source_promoted_to_another_account(
        self, manager, monkeypatch
    ):
        """An identity-pending verifier is keyed ``default`` when collection starts; a
        successful run promotes its source to the resolved account mid-attempt. The
        health update must reach the source's new row, not vanish against ``default``."""
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        from app.models.db import CredentialSource

        # The suite-wide autouse ``mock_db_session`` fixture replaces ``sqlmodel.Session``
        # (which ``_record_source_health`` imports at call time) with a mock; this test
        # needs a real in-memory DB, so put the real class back for its duration.
        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        with Session(engine) as session:
            session.add(
                CredentialSource(
                    provider_id="antigravity",
                    account_id="alice@example.com",  # promoted from "default"
                    source_id="sidecar:host-a:oauth",
                    source_type="file",
                    source_label="oauth.json",
                    sidecar_id="host-a",
                    health="unavailable",
                )
            )
            session.commit()

        manager._record_source_health("antigravity", "default", {"sidecar:host-a:oauth": "healthy"})
        # The failover cache is keyed by where the row lives now, not the pre-run id, or a
        # promoted source would keep its stale state until the next sync.
        assert manager._credential_source_state[("antigravity", "alice@example.com")] == {
            "sidecar:host-a:oauth": ("healthy", None)
        }
        assert ("antigravity", "default") not in manager._credential_source_state

        with Session(engine) as session:
            row = session.exec(select(CredentialSource)).one()
            assert row.health == "healthy"

    def test_record_source_health_stamps_provenance(self, manager, monkeypatch):
        """Each attempted source gets attempt/success/error provenance — the data behind
        "which source is the data coming from"."""
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        from app.models.db import CredentialSource

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        with Session(engine) as session:
            for source_id in ("sidecar:a", "sidecar:b"):
                session.add(
                    CredentialSource(
                        provider_id="gemini",
                        account_id="alice@example.com",
                        source_id=source_id,
                        source_type="file",
                        source_label=source_id,
                    )
                )
            session.commit()

        manager._record_source_health(
            "gemini",
            "alice@example.com",
            {"sidecar:a": "auth_failed", "sidecar:b": "healthy"},
        )

        with Session(engine) as session:
            rows = {r.source_id: r for r in session.exec(select(CredentialSource)).all()}
        assert rows["sidecar:a"].last_attempt_at is not None
        assert rows["sidecar:a"].last_success_at is None
        assert rows["sidecar:a"].last_error == "Authentication failed"
        assert rows["sidecar:b"].last_success_at is not None
        assert rows["sidecar:b"].last_error is None
        # Failover reads its own cache, so the outcome must be visible to the very next
        # cycle instead of after the next sync: a rejected source rests, a working one doesn't.
        state = manager._credential_source_state[("gemini", "alice@example.com")]
        assert state["sidecar:a"][0] == "auth_failed" and state["sidecar:a"][1] is not None
        assert state["sidecar:b"] == ("healthy", None)

    def test_record_server_sources_registers_the_env_credential_that_fed_collection(
        self, manager, monkeypatch
    ):
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        from app.models.db import CredentialSource

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")  # pragma: allowlist secret
        # Hermetic: ignore whatever gh / config files exist on the machine running the tests.
        monkeypatch.setattr("app.services.credential_provider._expand_rule_paths", lambda _p: [])

        manager._record_server_sources("github", "s3ntin3l8", "healthy")

        with Session(engine) as session:
            (row,) = session.exec(select(CredentialSource)).all()
        assert (row.provider_id, row.account_id) == ("github", "s3ntin3l8")
        assert row.source_type == "env" and row.source_label == "GITHUB_TOKEN"
        assert row.source_id == "server:github:env:GITHUB_TOKEN"
        assert row.last_success_at is not None

    def test_record_server_sources_registers_runways_device_login_file(self, manager, monkeypatch):
        """A managed file (the GitHub device-login token) is evidence for its account too."""
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        from app.models.db import CredentialSource
        from app.services.credential_provider import CredentialProvider

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        monkeypatch.setattr(
            CredentialProvider,
            "server_credential_origins",
            staticmethod(
                lambda _pid: [
                    {
                        "source_type": "file",
                        "label": "github_oauth.json",
                        "keys": ["api_key"],
                        "managed": True,
                    }
                ]
            ),
        )

        manager._record_server_sources("github", "s3ntin3l8", "healthy")

        with Session(engine) as session:
            (row,) = session.exec(select(CredentialSource)).all()
        assert (row.provider_id, row.account_id) == ("github", "s3ntin3l8")
        assert row.source_type == "file" and row.source_label == "github_oauth.json"

    def test_record_server_sources_prunes_a_removed_env_credential(self, manager, monkeypatch):
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        from app.models.db import CredentialSource

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        monkeypatch.setattr("app.services.credential_provider._expand_rule_paths", lambda _p: [])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")  # pragma: allowlist secret
        manager._record_server_sources("github", "s3ntin3l8", "healthy")

        # The env var is removed: the next collection must forget its row (even though no
        # server credential is left to register).
        monkeypatch.delenv("GITHUB_TOKEN")
        manager._record_server_sources("github", "default", "unavailable")

        with Session(engine) as session:
            assert session.exec(select(CredentialSource)).all() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "expect_outcome"),
        [
            ("complete", True),
            ("partial", True),  # fresh provider data, just not a complete snapshot
            ("failed", True),  # the credential was used and failed: record that
            ("cached", False),  # served from cache: nothing was verified
            ("skipped", False),  # collector not run
        ],
    )
    async def test_server_source_outcome_is_recorded_only_when_the_credential_was_used(
        self, manager, state, expect_outcome
    ):
        """A cached/skipped result must not stamp last_success_at on the env credential —
        the inventory would then show it as freshly verified when no collection ran."""
        smart = SimpleNamespace(
            collector=SimpleNamespace(PROVIDER_ID="github", account_id="s3ntin3l8"),
            last_collection_state=state,
        )
        manager.smart_collectors = {"github:default": smart}
        usable = [{"data_source": "api", "remaining": 5}]
        with (
            patch.object(manager, "_collect_with_source_failover", AsyncMock(return_value=usable)),
            patch.object(manager, "_record_source_health"),
            patch.object(manager, "_record_server_sources") as record,
        ):
            await manager._collect_with_semaphore("github:default", MagicMock())

        record.assert_called_once()
        assert record.call_args.args[:2] == ("github", "s3ntin3l8")
        assert record.call_args.args[2] == ("healthy" if expect_outcome else None)

    def test_record_server_sources_registers_without_stamping_when_nothing_was_verified(
        self, manager, monkeypatch
    ):
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        from app.models.db import CredentialSource

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        monkeypatch.setattr("app.services.credential_provider._expand_rule_paths", lambda _p: [])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")  # pragma: allowlist secret

        manager._record_server_sources("github", "s3ntin3l8", None)

        with Session(engine) as session:
            (row,) = session.exec(select(CredentialSource)).all()
        assert row.source_id == "server:github:env:GITHUB_TOKEN"
        assert (row.last_attempt_at, row.last_success_at, row.last_error) == (None, None, None)

    def test_record_server_sources_failure_is_a_warning_not_an_exception(
        self, manager, monkeypatch, caplog
    ):
        """Never break a collection over provenance — but a persistent failure must be
        visible above debug level."""
        import logging

        def boom(_provider_id):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(
            "app.services.credential_provider.CredentialProvider.get_credentials",
            staticmethod(boom),
        )
        with caplog.at_level(logging.WARNING, logger="app.services.collector_manager"):
            manager._record_server_sources("github", "default", "healthy")  # must not raise

        assert any(
            r.levelno == logging.WARNING and "server credential sources" in r.getMessage()
            for r in caplog.records
        )

    def test_result_health_collapses_collector_output(self, manager):
        usable = [{"data_source": "api", "remaining": 5}]
        assert manager._result_health(usable) == "healthy"
        assert manager._result_health([{"error_type": "auth_failed"}]) == "auth_failed"
        assert manager._result_health([{"error_type": "invalid_api_key"}]) == "auth_failed"
        assert manager._result_health([{"error_type": "api_error"}]) == "unavailable"
        assert manager._result_health([]) == "unavailable"

    @pytest.mark.asyncio
    async def test_disabled_only_account_pops_dynamic_collector(self, manager):
        """Disabling the sole (non-default) account must remove its collector."""
        manager.smart_collectors = {"anthropic:alice@example.com": MagicMock()}

        class _Cfg:
            provider_id = "anthropic"
            account_id = "alice@example.com"
            enabled = False  # the account was just disabled
            archived = False
            poll_interval_seconds = None
            account_label = None
            strategies = None
            api_key = None
            session_cookie = None

        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
            ) as mock_accounts,
            patch("sqlmodel.Session") as mock_session_cls,
        ):
            # Cache still holds the credential (disable doesn't purge cache).
            mock_accounts.return_value = [("anthropic", "alice@example.com", "Alice")]
            inner = MagicMock()
            inner.exec.return_value.all.side_effect = [
                [_Cfg()],  # only the disabled email row
                [],
            ]
            inner.exec.return_value.first.return_value = None
            mock_session_cls.return_value.__enter__.return_value = inner

            manager._last_sync_time = 0
            await manager._sync_collectors(force=True)

        assert "anthropic:alice@example.com" not in manager.smart_collectors
        assert "anthropic:default" not in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_archived_only_provider_ignores_sidecar_accounts_and_pending_sources(
        self, manager
    ):
        """Sidecar credentials cannot reactivate a provider after all accounts are archived."""
        manager.smart_collectors = {
            "gemini:default": MagicMock(),
            "gemini:default:identity-pending": MagicMock(),
            "gemini:new@example.com": MagicMock(),
        }

        archived = SimpleNamespace(
            provider_id="gemini",
            account_id="old@example.com",
            enabled=False,
            archived=True,
            poll_interval_seconds=None,
            account_label=None,
            strategies=None,
            api_key=None,
            session_cookie=None,
        )

        async def source_candidates(provider_id, _account_id):
            if provider_id == "gemini":
                return [
                    {
                        "source_id": "sidecar:hermes-01:gemini-auth",
                        "source_type": "sidecar",
                        "credential_origin": "path:/home/user/.gemini/oauth_creds.json",
                        "identity_pending": True,
                    }
                ]
            return []

        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
                return_value=[("gemini", "new@example.com", "New")],
            ),
            patch(
                "app.services.collector_manager.token_cache.get_source_candidates",
                side_effect=source_candidates,
            ),
            patch("sqlmodel.Session") as session_cls,
        ):
            inner = MagicMock()
            inner.exec.return_value.all.side_effect = [[archived], []]
            inner.exec.return_value.first.return_value = None
            session_cls.return_value.__enter__.return_value = inner
            manager.reconcile_token_cache_from_durable_tags = AsyncMock()

            await manager._sync_collectors(force=True)

        assert not any(key.startswith("gemini:") for key in manager.smart_collectors)

    @pytest.mark.asyncio
    async def test_unresolved_identity_failure_does_not_create_default_stale_outcome(self, manager):
        collector = SimpleNamespace(PROVIDER_ID="gemini", account_id="default")
        smart = MagicMock(collector=collector, last_collection_state="failed")
        manager.smart_collectors = {"gemini:default:identity-pending": smart}

        async def sync(_force=False):
            return None

        async def fail(_key, _client):
            raise RuntimeError("upstream unavailable")

        manager._sync_collectors = sync
        manager._get_client = AsyncMock(return_value=MagicMock())
        manager._collect_with_semaphore = fail

        await manager._do_collect()

        assert manager.last_collection_outcomes == []

    @pytest.mark.asyncio
    async def test_a_verifier_that_adopted_an_email_records_no_server_outcome_for_it(self, manager):
        """After proving an email the verifier's collector carries that account id; its state is
        the pending source's, not that account's own server credential."""
        collector = SimpleNamespace(PROVIDER_ID="chatgpt", account_id="alice@example.com")
        smart = MagicMock(collector=collector, last_collection_state="failed")
        manager.smart_collectors = {"chatgpt:default:identity-pending": smart}

        async def sync(_force=False):
            return None

        async def done(_key, _client):
            return []

        manager._sync_collectors = sync
        manager._get_client = AsyncMock(return_value=MagicMock())
        manager._collect_with_semaphore = done

        await manager._do_collect()

        assert manager.last_collection_outcomes == []

    @pytest.mark.asyncio
    async def test_step2_does_not_respawn_default_from_cache(self, manager):
        """Sidecar may stamp the token cache with account_id="default"
        (`_gemini_account_email` / `_ag_account_email` fallbacks). When config
        rows exist without a default sentinel, step 2 must not spawn
        {pid}:default — that would re-collect after the user disabled the
        only account (Hermes review on PR #309)."""
        manager.smart_collectors = {}  # step 1 never spawned the default

        class _Cfg:
            provider_id = "anthropic"
            account_id = "alice@example.com"
            enabled = False  # sole account disabled
            archived = False
            poll_interval_seconds = None
            account_label = None
            strategies = None
            api_key = None
            session_cookie = None

        with (
            patch(
                "app.services.collector_manager.token_cache.get_all_active_accounts",
                new_callable=AsyncMock,
            ) as mock_accounts,
            patch("sqlmodel.Session") as mock_session_cls,
        ):
            # Cache still holds a literal "default" identity (sidecar fallback).
            mock_accounts.return_value = [
                ("anthropic", "default", "default"),
                ("anthropic", "alice@example.com", "Alice"),
            ]
            inner = MagicMock()
            inner.exec.return_value.all.side_effect = [
                [_Cfg()],
                [],
            ]
            inner.exec.return_value.first.return_value = None
            mock_session_cls.return_value.__enter__.return_value = inner

            manager._last_sync_time = 0
            await manager._sync_collectors(force=True)

        assert "anthropic:default" not in manager.smart_collectors
        assert "anthropic:alice@example.com" not in manager.smart_collectors


class TestDefaultCollectorSourceSweep:
    """The default collector must find bundles filed under a resolved identity.

    Sidecar ingest keys source bundles by the account it proved (the email from
    userinfo), never by ``default``. Without a sweep, ``_source_candidates``
    returns nothing for the default slot and the failover loop silently
    degrades to a single unpinned ``collect()``: no cross-source failover, and
    reads that depend on the merged cache alone — the 2026-10-02 Antigravity
    incident, where the default collector 401'd on a dead merged token while a
    live source bundle sat unused under the identity slot.
    """

    @pytest.mark.asyncio
    async def test_default_collector_pins_to_identity_keyed_bundle(self, manager, monkeypatch):
        from app.services.token_cache import token_cache

        email = "s3ntin3l8@gmail.com"
        source_id = "sidecar:mgmt:path:/home/user/antigravity-oauth-token"
        now_ms = str(int(time.time() * 1000) + 3_600_000)
        await token_cache.store(
            "antigravity",
            {"oauth_token": "ya29.live", "expiry_date": now_ms},
            account_id=email,
            account_label=email,
            source="mgmt",
            source_id=source_id,
            source_metadata={
                "source_type": "sidecar",
                "credential_origin": "path:/home/user/antigravity-oauth-token",
                "sidecar_id": "mgmt",
            },
        )

        collector = SimpleNamespace(
            PROVIDER_ID="antigravity",
            account_id="default",
            account_label="Default",
            credential_account_id="default",
        )
        smart = MagicMock(collector=collector)
        smart.reset = AsyncMock()
        pinned: list[tuple[str, str] | None] = []

        async def collect(_client):
            pinned.append(token_cache.selected_source("antigravity"))
            collector.account_id = email  # userinfo resolution inside the real collector
            return [{"service_name": "Antigravity", "remaining": 7}]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["antigravity:default"] = smart
        promote = AsyncMock()
        monkeypatch.setattr(manager, "_promote_source_identity", promote)
        monkeypatch.setattr(manager, "_persist_identity_pending_preview", MagicMock())
        health = {}

        result = await manager._collect_with_source_failover(
            "antigravity:default", MagicMock(), health
        )

        assert result == [{"service_name": "Antigravity", "remaining": 7}]
        # Pinned to the bundle (account_slot = resolved identity), never unpinned:
        assert pinned == [(email, source_id)]
        assert health[source_id] == "healthy"
        promote.assert_awaited_once_with(
            "antigravity",
            "default",
            source_id,
            email,
            cache_account_id=email,
        )

    @pytest.mark.asyncio
    async def test_identified_account_never_sweeps_other_accounts_bundles(
        self, manager, monkeypatch
    ):
        """A sweep must only rescue the default slot: alice's collector with no
        bundles of her own stays unpinned (today's behavior) and must never
        borrow bob's credentials."""
        from app.services.token_cache import token_cache

        await token_cache.store(
            "antigravity",
            {"oauth_token": "ya29.bob", "expiry_date": str(int(time.time() * 1000) + 3_600_000)},
            account_id="bob@example.com",
            source="mgmt",
            source_id="sidecar:mgmt:path:/home/bob/antigravity-oauth-token",
            source_metadata={
                "source_type": "sidecar",
                "credential_origin": "path:/home/bob/antigravity-oauth-token",
                "sidecar_id": "mgmt",
            },
        )

        collector = SimpleNamespace(
            PROVIDER_ID="antigravity",
            account_id="alice@example.com",
            account_label="Alice",
            credential_account_id="alice@example.com",
        )
        smart = MagicMock(collector=collector)
        smart.reset = AsyncMock()
        pinned: list[tuple[str, str] | None] = []

        async def collect(_client):
            pinned.append(token_cache.selected_source("antigravity"))
            return [{"service_name": "Antigravity", "remaining": 3}]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["antigravity:alice@example.com"] = smart
        health = {}

        result = await manager._collect_with_source_failover(
            "antigravity:alice@example.com", MagicMock(), health
        )

        assert result == [{"service_name": "Antigravity", "remaining": 3}]
        assert pinned == [None]
        assert health == {}

    def test_default_slot_preference_applies_to_swept_candidate(self, manager):
        """A preference stored against the requesting account must apply to
        swept candidates.

        Ingest files bundles under the resolved identity, but an operator may
        configure sources from the default account view: that row must not be
        silently ignored just because every Antigravity candidate carries an
        ``account_slot``. The slot's own row wins when both exist (it is the
        more specific one). Priorities sort ascending — lower is tried first.
        """
        manager.set_credential_source_preferences(
            "antigravity",
            "default",
            {
                "src:disabled": (False, 0),
                "src:default-view": (True, 3),
                "src:pinned": (True, 50),
            },
        )
        manager.set_credential_source_preferences(
            "antigravity", "user@example.com", {"src:pinned": (True, 0)}
        )
        swept = [
            {
                "source_id": "src:disabled",
                "account_slot": "user@example.com",
                "enabled": True,
                "priority": 1,
            },
            {
                "source_id": "src:default-view",
                "account_slot": "user@example.com",
                "enabled": True,
                "priority": 1,
            },
            {
                "source_id": "src:pinned",
                "account_slot": "user@example.com",
                "enabled": True,
                "priority": 99,
            },
        ]

        ordered = manager._ordered_candidates("antigravity", "default", swept)

        # src:disabled dropped by the default-view row (that row applies to a
        # swept candidate at all); src:pinned is ordered by its slot row (0),
        # which also outranks the default row's 50 and its own 99.
        assert [candidate["source_id"] for candidate in ordered] == [
            "src:pinned",
            "src:default-view",
        ]

    async def test_rejected_source_rests_between_retries_then_comes_due_in_its_normal_place(
        self, manager
    ):
        from datetime import UTC, datetime, timedelta

        def cand(source_id: str, priority: int) -> dict:
            return {"source_id": source_id, "enabled": True, "priority": priority}

        candidates = [cand("src:revoked", 0), cand("src:good", 1), cand("src:other", 2)]
        later = datetime.now(UTC) + timedelta(hours=1)
        manager._credential_source_state = {
            ("anthropic", "default"): {"src:revoked": ("auth_failed", later)}
        }
        # Resting: the revoked key is not tried this cycle, and it no longer leads.
        ordered = manager._ordered_candidates("anthropic", "default", candidates)
        assert [c["source_id"] for c in ordered] == ["src:good", "src:other"]

        # Rest is over: it is due, so it is tried in its normal place. Demoting it instead
        # would starve it — failover stops at the first source that works.
        manager._credential_source_state = {
            ("anthropic", "default"): {
                "src:revoked": ("auth_failed", datetime.now(UTC) - timedelta(seconds=1))
            }
        }
        ordered = manager._ordered_candidates("anthropic", "default", candidates)
        assert [c["source_id"] for c in ordered] == ["src:revoked", "src:good", "src:other"]

    def test_a_collector_that_legitimately_reports_nothing_is_not_failing(self, manager):
        assert manager._result_health([], empty_allowed=True) == "healthy"
        assert manager._result_health([]) == "unavailable"
        assert manager._result_health([{"error_type": "api_error"}], empty_allowed=True) == (
            "unavailable"
        )

    def test_a_reset_rest_is_forgotten_by_the_failover_cache_too(self, manager):
        from datetime import UTC, datetime, timedelta

        later = datetime.now(UTC) + timedelta(hours=3)
        manager._credential_source_state = {
            ("anthropic", "alice@example.com"): {"src:a": ("auth_failed", later)},
            ("gemini", "alice@example.com"): {"src:a": ("auth_failed", later)},
        }
        manager.clear_source_retry("anthropic", "src:a")
        assert manager._credential_source_state[("anthropic", "alice@example.com")]["src:a"] == (
            "auth_failed",
            None,
        )
        assert manager._credential_source_state[("gemini", "alice@example.com")]["src:a"][1] == (
            later
        )

    async def test_the_last_credential_standing_is_never_starved_by_its_rest_period(self, manager):
        from datetime import UTC, datetime, timedelta

        later = datetime.now(UTC) + timedelta(hours=1)
        candidates = [
            {"source_id": "src:a", "enabled": True, "priority": 0},
            {"source_id": "src:b", "enabled": True, "priority": 1},
        ]
        manager._credential_source_state = {
            ("anthropic", "default"): {
                "src:a": ("auth_failed", later),
                "src:b": ("auth_failed", later),
            }
        }
        ordered = manager._ordered_candidates("anthropic", "default", candidates)
        assert [c["source_id"] for c in ordered] == ["src:a"]  # one probe, best first

    async def test_all_pending_sweep_falls_back_to_unpinned_collect(self, manager, monkeypatch):
        """Identity-pending rows never reach the default collector's failover.

        When the sweep's only candidates are pending, the default collector
        falls back to the unpinned merged-cache read — not ``[]``, not a pin —
        and the dropped rows record no health: the verifier owns them.
        """
        collector = SimpleNamespace(
            PROVIDER_ID="antigravity",
            account_id="default",
            account_label="Default",
            credential_account_id="default",
        )
        smart = MagicMock(collector=collector)
        smart.reset = AsyncMock()

        async def collect(_client):
            return [{"service_name": "Antigravity", "remaining": 5}]

        smart.collect = AsyncMock(side_effect=collect)
        manager.smart_collectors["antigravity:default"] = smart

        async def no_slot_sources(*_args):
            return []

        swept_seen: list[dict] = []

        async def only_pending(*_args):
            row = {
                "source_id": "sidecar:host:path:/x",
                "identity_pending": True,
                "account_slot": "someone@example.com",
            }
            swept_seen.append(row)
            return [row]

        def _no_pin(*_args, **_kwargs):
            raise AssertionError(
                "a pending source must never be attempted by the default collector"
            )

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_source_candidates", no_slot_sources
        )
        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_account_source_candidates",
            only_pending,
        )
        monkeypatch.setattr("app.services.collector_manager.token_cache.using_source", _no_pin)
        health = {}

        result = await manager._collect_with_source_failover(
            "antigravity:default", MagicMock(), health
        )

        assert result == [{"service_name": "Antigravity", "remaining": 5}]
        smart.collect.assert_awaited_once()
        assert health == {}
        assert collector.credential_account_id == "default"
        # The regression only bites when the sweep yields nothing but pending
        # rows: exactly one row, and it is identity-pending.
        assert len(swept_seen) == 1
        assert swept_seen[0]["identity_pending"] is True

    async def test_sweep_dedupes_source_ids_already_in_the_slot(self, monkeypatch):
        shared = {"source_id": "src:dup", "account_slot": "default"}
        only_swept = {"source_id": "src:swept", "account_slot": "user@example.com"}

        async def slot_rows(*_args):
            return [shared]

        async def swept_rows(*_args):
            return [shared, only_swept]

        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_source_candidates", slot_rows
        )
        monkeypatch.setattr(
            "app.services.collector_manager.token_cache.get_account_source_candidates",
            swept_rows,
        )

        candidates = await CollectorManager._source_candidates("antigravity", "default", False)

        assert [candidate["source_id"] for candidate in candidates] == ["src:dup", "src:swept"]


class TestCollectorManagerWarmup:
    @pytest.mark.skip(reason="keychain warmup removed; keychain access moved to sidecar")
    @pytest.mark.asyncio
    async def test_warmup_keychain_non_darwin(self, manager):
        pass

    @pytest.mark.skip(reason="keychain warmup removed; keychain access moved to sidecar")
    @pytest.mark.asyncio
    async def test_warmup_keychain_disabled(self, manager):
        pass


class TestCollectorManagerCollection:
    @pytest.mark.asyncio
    async def test_global_timeout_cancellation_is_a_failed_outcome(self, manager):
        smart = MagicMock()
        smart.collector.PROVIDER_ID = "anthropic"
        smart.collector.account_id = "alice@example.com"
        smart.last_collection_state = "complete"
        manager.smart_collectors = {"anthropic:alice@example.com": smart}

        async def wait_forever(_key, _client):
            await asyncio.Event().wait()

        with (
            patch.object(manager, "_sync_collectors", new_callable=AsyncMock),
            patch.object(manager, "_get_client", new_callable=AsyncMock),
            patch.object(manager, "_collect_with_semaphore", side_effect=wait_forever),
            patch(
                "app.services.collector_manager.asyncio.wait",
                new_callable=AsyncMock,
                side_effect=lambda tasks, timeout: (set(), set(tasks)),
            ),
        ):
            result = await manager._do_collect()

        assert result == []
        assert manager.last_collection_outcomes == [
            {
                "provider_id": "anthropic",
                "account_id": "alice@example.com",
                "source_id": "server:anthropic",
                "state": "failed",
                "reason": "collection timed out",
            }
        ]

    @pytest.mark.asyncio
    async def test_collect_all_success(self, manager):
        """Test successful collection flow."""
        # Use simple mock collectors
        mock_sc1 = AsyncMock()
        mock_sc1.collect.return_value = [{"service_name": "S1"}]
        mock_sc2 = AsyncMock()
        mock_sc2.collect.return_value = [{"service_name": "S2"}]

        manager.smart_collectors = {"c1:default": mock_sc1, "c2:default": mock_sc2}

        # Mock dependencies
        with patch.object(manager, "_sync_collectors", new_callable=AsyncMock):
            # Run collection (external_metric_service removed; all data comes from collectors)
            results = await manager.collect_all()

            assert len(results) == 2
            services = [r["service_name"] for r in results]
            assert "S1" in services
            assert "S2" in services

    @pytest.mark.asyncio
    async def test_collect_all_timeout(self, manager):
        """Test that global timeout is handled gracefully."""

        # Mock _do_collect to simulate a timeout or empty result
        async def mock_do_collect():
            return []

        with patch.object(manager, "_do_collect", side_effect=mock_do_collect):
            # Reset future to ensure leader logic runs
            manager._collect_future = None
            results = await manager.collect_all()
            assert results == []

    @pytest.mark.asyncio
    async def test_collect_all_handles_exceptions(self, manager):
        """Test that exceptions in one collector don't crash everything."""

        # Use a simple mock for _do_collect to verify it returns results correctly
        async def mock_do_collect():
            return [{"service_name": "OK"}]

        with patch.object(manager, "_do_collect", side_effect=mock_do_collect):
            # Reset future to ensure leader logic runs
            manager._collect_future = None
            results = await manager.collect_all()

            assert len(results) == 1
            assert results[0]["service_name"] == "OK"


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


def test_keep_alive_providers_are_the_ones_with_a_sidecar_renewer():
    """The server's keep-alive hint must cover exactly what the sidecar can renew."""
    from app.services.refresh_policy import KEEP_ALIVE_PROVIDERS
    from scripts import sidecar
    from scripts.sidecar_pkg import keep_alive
    from scripts.sidecar_pkg.anthropic_renewer import AnthropicRenewer
    from scripts.sidecar_pkg.codex_renewer import CodexRenewer
    from scripts.sidecar_pkg.xai_renewer import XaiRenewer

    thread = sidecar._make_keep_alive_thread()
    renewer_providers = {r.name for r in thread._renewers} | {keep_alive.AGY_PROVIDER}
    assert renewer_providers == {
        XaiRenewer.name,
        AnthropicRenewer.name,
        CodexRenewer.name,
        "antigravity",
    }
    assert KEEP_ALIVE_PROVIDERS == renewer_providers


class TestOutcomeReasons:
    """Every outcome carries a reason: the stale card's explanation for any provider."""

    async def _outcomes(self, manager, *, state, reason, raises=None):
        smart = MagicMock()
        smart.collector.PROVIDER_ID = "gemini"
        smart.collector.account_id = "alice@example.com"
        smart.last_collection_state = state
        smart.last_collection_reason = reason
        manager.smart_collectors = {"gemini:alice@example.com": smart}

        async def collect(_key, _client):
            if raises:
                raise raises
            return []

        with (
            patch.object(manager, "_sync_collectors", new_callable=AsyncMock),
            patch.object(manager, "_get_client", new_callable=AsyncMock),
            patch.object(manager, "_collect_with_semaphore", side_effect=collect),
        ):
            await manager._do_collect()
        return manager.last_collection_outcomes

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            ("failed", "Token expired — run `agy`"),
            ("failed", "provider rate limited"),
            ("skipped", "login expired — waiting for its machine to renew it"),
            ("skipped", "collector is not configured"),
            ("complete", "fresh provider response"),
        ],
    )
    async def test_reason_is_the_collectors_own(self, manager, state, reason):
        (outcome,) = await self._outcomes(manager, state=state, reason=reason)
        assert outcome["state"] == state
        assert outcome["reason"] == reason

    @pytest.mark.asyncio
    async def test_an_exception_names_its_type(self, manager):
        (outcome,) = await self._outcomes(
            manager, state="complete", reason="x", raises=RuntimeError("boom")
        )
        assert outcome["state"] == "failed"
        assert outcome["reason"] == "collection raised RuntimeError"
        assert "boom" not in outcome["reason"]  # the message may carry secrets/PII


class TestRenewalWaitReason:
    """What a stale card says about an expired login its machine must renew."""

    @pytest.fixture
    def reported(self, manager, monkeypatch):
        """Pretend each sidecar reports the given keep-alive state: sidecar_id -> reported.

        ``reported.desired[sidecar_id]`` sets the server-side override (default: none).
        """

        class State(dict):
            desired: dict[str, bool | None]

        state = State()
        state.desired = {}
        monkeypatch.setattr(
            manager,
            "_read_sidecar_keep_alive",
            lambda sid, provider=None: (state.get(sid), state.desired.get(sid)),
            raising=False,
        )
        return state

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["xai", "antigravity", "anthropic"])
    @pytest.mark.parametrize("value", [False, None])
    async def test_suggests_keep_alive_when_it_is_off_or_unknown(
        self, manager, reported, provider, value
    ):
        reported["host-a"] = value
        reason = await manager._renewal_wait_reason(provider, {"sidecar_id": "host-a"})
        assert "--keep-alive" in reason and "Fleet" in reason

    @pytest.mark.asyncio
    async def test_unknown_sidecar_is_treated_as_not_reporting(self, manager, reported):
        assert "--keep-alive" in await manager._renewal_wait_reason("xai", {"sidecar_id": "ghost"})
        assert "--keep-alive" in await manager._renewal_wait_reason("xai", {})

    @pytest.mark.asyncio
    async def test_keep_alive_on_points_at_the_sidecar_not_the_flag(self, manager, reported):
        reported["host-a"] = True
        reason = await manager._renewal_wait_reason("xai", {"sidecar_id": "host-a"})
        assert "--keep-alive" not in reason
        assert "hasn't renewed" in reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [False, None])
    async def test_a_pending_request_is_not_re_advised(self, manager, reported, value):
        """Already switched on in Fleet but the sidecar hasn't checked in yet: don't tell the
        operator to turn it on again."""
        reported["host-a"] = value
        reported.desired["host-a"] = True
        reason = await manager._renewal_wait_reason("xai", {"sidecar_id": "host-a"})
        assert "--keep-alive" not in reason
        assert "next check-in" in reason and "switched on" in reason

    @pytest.mark.asyncio
    async def test_the_fleet_default_counts_as_switched_on(self, manager, reported, monkeypatch):
        """Turned on fleet-wide but this sidecar still reports off: it applies on its next
        check-in, so don't tell the operator to turn it on again."""
        monkeypatch.setattr(manager, "_read_keep_alive_fleet_default", lambda: True)
        reported["host-a"] = False
        reason = await manager._renewal_wait_reason("xai", {"sidecar_id": "host-a"})
        assert "--keep-alive" not in reason and "next check-in" in reason

    @pytest.mark.asyncio
    async def test_an_explicit_off_override_beats_the_fleet_default(
        self, manager, reported, monkeypatch
    ):
        monkeypatch.setattr(manager, "_read_keep_alive_fleet_default", lambda: True)
        reported["host-a"] = False
        reported.desired["host-a"] = False
        reason = await manager._renewal_wait_reason("xai", {"sidecar_id": "host-a"})
        assert "--keep-alive" in reason and "Fleet" in reason

    @pytest.mark.asyncio
    async def test_the_fleet_default_cannot_help_a_sidecar_that_never_reports(
        self, manager, reported, monkeypatch
    ):
        """The tray app / an older sidecar doesn't run keep-alive at all."""
        monkeypatch.setattr(manager, "_read_keep_alive_fleet_default", lambda: True)
        reported["host-a"] = None
        reason = await manager._renewal_wait_reason("xai", {"sidecar_id": "host-a"})
        assert "--keep-alive" in reason

    @pytest.mark.asyncio
    async def test_a_request_to_turn_it_off_still_advises_turning_it_on(self, manager, reported):
        reported["host-a"] = False
        reported.desired["host-a"] = False
        reason = await manager._renewal_wait_reason("xai", {"sidecar_id": "host-a"})
        assert "--keep-alive" in reason and "Fleet" in reason

    @pytest.mark.asyncio
    async def test_running_keep_alive_wins_over_a_pending_off_request(self, manager, reported):
        reported["host-a"] = True
        reported.desired["host-a"] = False
        reason = await manager._renewal_wait_reason("xai", {"sidecar_id": "host-a"})
        assert "hasn't renewed" in reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["gemini", "opencode", None])
    async def test_other_providers_get_no_keep_alive_advice(self, manager, reported, provider):
        reason = await manager._renewal_wait_reason(provider, {"sidecar_id": "host-a"})
        assert "keep-alive" not in reason

    @pytest.mark.asyncio
    async def test_a_fleet_toggle_shows_up_on_the_very_next_call(self, manager, monkeypatch):
        """The flag is read at skip time, not cached at sync: no one-sync lag after a toggle."""
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        from app.core import db as core_db
        from app.models.db import SidecarRegistry

        eng = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(eng)
        monkeypatch.setattr(core_db, "engine", eng)
        # The suite-wide autouse fixture mocks ``sqlmodel.Session``; this needs a real DB.
        monkeypatch.setattr("sqlmodel.Session", Session)
        with Session(eng) as s:
            s.add(SidecarRegistry(sidecar_id="host-a", hostname="a", keep_alive=False))
            s.commit()

        candidate = {"sidecar_id": "host-a"}
        assert "--keep-alive" in await manager._renewal_wait_reason("xai", candidate)

        with Session(eng) as s:
            row = s.get(SidecarRegistry, "host-a")
            row.keep_alive = True
            s.add(row)
            s.commit()

        assert "hasn't renewed" in await manager._renewal_wait_reason("xai", candidate)

        with Session(eng) as s:
            row = s.get(SidecarRegistry, "host-a")
            row.keep_alive = False
            row.keep_alive_desired = True
            s.add(row)
            s.commit()

        assert "switched on" in await manager._renewal_wait_reason("xai", candidate)

    @pytest.mark.asyncio
    async def test_the_reason_reads_the_logins_own_state(self, manager, monkeypatch):
        """Keep-alive is on for the sidecar but the operator switched xAI off: xAI's stale card
        must advise turning it on, while Codex's (still on) says keep-alive hasn't renewed it."""
        import json

        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        from app.core import db as core_db
        from app.models.db import SidecarRegistry

        eng = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(eng)
        monkeypatch.setattr(core_db, "engine", eng)
        monkeypatch.setattr("sqlmodel.Session", Session)
        with Session(eng) as s:
            s.add(
                SidecarRegistry(
                    sidecar_id="host-a",
                    hostname="a",
                    keep_alive=True,
                    keep_alive_providers=json.dumps({"xai": False, "chatgpt": True}),
                    keep_alive_desired_providers=json.dumps({"xai": False}),
                )
            )
            s.commit()

        candidate = {"sidecar_id": "host-a"}
        assert "--keep-alive" in await manager._renewal_wait_reason("xai", candidate)
        assert "hasn't renewed" in await manager._renewal_wait_reason("chatgpt", candidate)
        assert manager._read_sidecar_keep_alive("host-a") == (True, None)  # sidecar-level
        assert manager._read_sidecar_keep_alive("host-a", "xai") == (False, False)

    def test_a_database_error_reads_as_unknown(self, manager, monkeypatch):
        def broken_session(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr("sqlmodel.Session", broken_session)
        assert manager._read_sidecar_keep_alive("host-a") == (None, None)
        assert manager._read_sidecar_keep_alive("") == (None, None)
