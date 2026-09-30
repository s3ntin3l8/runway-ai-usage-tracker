import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
            "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
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
                "antigravity", "default", candidate["source_id"], resolved_account
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

        async def source_candidates(provider_id, account_id):
            if provider_id == "antigravity" and account_id == "default":
                return [
                    {
                        "source_id": "sidecar:laptop:auth-json",
                        "source_type": "sidecar",
                        "credential_origin": "path:/auth.json",
                        "identity_pending": True,
                    }
                ]
            return []

        with patch(
            "app.services.collector_manager.token_cache.get_source_candidates",
            side_effect=source_candidates,
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
            "app.services.collector_manager.token_cache.get_source_candidates", get_candidates
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
            "antigravity", "default", candidates[1]["source_id"], "bob@example.com"
        )

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
