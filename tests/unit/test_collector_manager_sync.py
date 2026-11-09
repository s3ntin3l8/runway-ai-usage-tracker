"""Unit tests for CollectorManager registration, synchronization, and account promotion."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session as RealSession

from app.services.collectors.base import BaseCollector
from tests.unit._collector_manager_helpers import manager_fixture  # noqa: F401 — fixture


class TestCollectorManagerInitialization:
    def test_init_registry_count(self, manager):
        """Test that default registry contains expected providers."""
        # 16 providers, including the independent Claude Console connection.
        assert len(manager.collector_registry) == 16
        assert "anthropic" in manager.collector_registry
        assert "anthropic_console" in manager.collector_registry
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
        monkeypatch.setattr(manager, "promote_source_identity", promote)
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

        await manager.sync_collectors()

        # Check that some default collectors are present
        assert "anthropic:default" in manager.smart_collectors
        assert "gemini:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_default_collectors_pin_credential_account_id(self, manager):
        """Auth-failure flagging attributes a rejected default key to `default`,
        not to whatever identity the collector resolved for its cards."""
        manager.smart_collectors = {}

        await manager.sync_collectors()

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
            await manager.sync_collectors(force=True)

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
                    user_strategies=None,
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
            await manager.sync_collectors(force=True)

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
        monkeypatch.setattr(manager, "promote_source_identity", promote)

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

            await manager.sync_collectors(force=True)

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

            await manager.sync_collectors(force=True)

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
            manager.last_sync_time = 0
            await manager.sync_collectors()

            assert "anthropic:stale-account" not in manager.smart_collectors
            # Defaults should remain
            assert "anthropic:default" in manager.smart_collectors

    @pytest.mark.asyncio
    async def test_sync_collectors_force_bypasses_throttle(self, manager):
        """Config mutations pass force=True and must not wait out the 60s throttle."""
        manager.smart_collectors = {}
        manager.last_sync_time = time.time()  # just synced — throttle would skip

        await manager.sync_collectors()  # non-forced → skipped by throttle
        assert manager.smart_collectors == {}

        await manager.sync_collectors(force=True)
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
            # Order inside sync_collectors: ProviderConfig.all(),
            # SystemConfig.first(), LatestUsage.all().
            inner.exec.return_value.all.side_effect = [
                [_Cfg()],  # ProviderConfig rows
                [],  # LatestUsage identities
            ]
            inner.exec.return_value.first.return_value = None  # SystemConfig
            mock_session_cls.return_value.__enter__.return_value = inner

            manager.last_sync_time = 0
            await manager.sync_collectors(force=True)

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

            manager.last_sync_time = 0
            await manager.sync_collectors(force=True)

        bob = manager.smart_collectors["github:bob"]
        carol = manager.smart_collectors["github:carol"]
        assert bob.collector.account_label != "alice@example.com"
        assert bob.ttl == 123  # non-label fallbacks still come from ``default``
        assert carol.collector.account_label == "carol@example.com"

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

            manager.last_sync_time = 0
            await manager.sync_collectors(force=True)

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

            await manager.sync_collectors(force=True)

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

        manager.sync_collectors = sync
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

        manager.sync_collectors = sync
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

            manager.last_sync_time = 0
            await manager.sync_collectors(force=True)

        assert "anthropic:default" not in manager.smart_collectors
        assert "anthropic:alice@example.com" not in manager.smart_collectors


@pytest.mark.asyncio
@pytest.mark.parametrize("promotion_failure", ["missing_row", "operator_same", "operator_other"])
async def test_non_claude_promotion_failure_keeps_legacy_collected_cards(
    manager, monkeypatch, promotion_failure
):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel, create_engine, select

    from app.models.db import CredentialSource
    from app.services.credential_tags import CredentialTagRepo
    from app.services.token_cache import TokenCache

    Session = RealSession
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    cache = TokenCache()
    source_id = "sidecar:legacy-source"
    origin = "path:/auth.json"
    if promotion_failure != "missing_row":
        with Session(engine) as session:
            session.add(
                CredentialSource(
                    provider_id="antigravity",
                    account_id="default",
                    source_id=source_id,
                    source_type="file",
                    source_label="host",
                    sidecar_id="host",
                    credential_origin=origin,
                )
            )
            CredentialTagRepo.set_tag(
                session,
                provider_id="antigravity",
                credential_origin=origin,
                account_id="verified@example.com",
                sidecar_id="host",
                set_by="operator",
            )
            session.commit()
    monkeypatch.setattr("app.core.db.engine", engine)
    monkeypatch.setattr("sqlmodel.Session", Session)
    monkeypatch.setattr("app.services.collector_manager.token_cache", cache)
    monkeypatch.setattr("app.services.collector_failover.token_cache", cache)
    await cache.store(
        "antigravity",
        {"oauth_token": "fake"},
        account_id="default",
        source_id=source_id,
        source="host",
        source_metadata={
            "identity_pending": True,
            "source_type": "file",
            "sidecar_id": "host",
            "credential_origin": origin,
        },
    )
    collector = SimpleNamespace(
        PROVIDER_ID="antigravity",
        account_id="default",
        account_label=None,
        credential_account_id="default",
    )
    smart = MagicMock(collector=collector)
    smart.reset = AsyncMock()

    async def collect(_client):
        collector.account_id = "verified@example.com"
        if promotion_failure == "operator_other":
            # An operator reassigned this exact source while collection was in flight.
            with Session(engine) as session:
                CredentialTagRepo.set_tag(
                    session,
                    provider_id="antigravity",
                    credential_origin=origin,
                    account_id="operator@example.com",
                    sidecar_id="host",
                    set_by="operator",
                )
                source = session.exec(select(CredentialSource)).one()
                source.account_id = "operator@example.com"
                session.add(source)
                session.commit()
        return [{"service_name": "Antigravity", "remaining": 7}]

    smart.collect = AsyncMock(side_effect=collect)
    manager.smart_collectors["antigravity:default:identity-pending"] = smart
    result = await manager._collect_with_source_failover(
        "antigravity:default:identity-pending", MagicMock(), {}
    )
    if promotion_failure == "operator_other":
        assert result == []
        smart.set_collection_state.assert_called_once_with(
            "skipped", "credential source assigned to another account"
        )
    else:
        assert result == [{"service_name": "Antigravity", "remaining": 7}]
        smart.set_collection_state.assert_not_called()
    if promotion_failure != "missing_row":
        with Session(engine) as session:
            assert CredentialTagRepo.get(
                session, provider_id="antigravity", credential_origin=origin, sidecar_id="host"
            ).account_id == (
                "verified@example.com"
                if promotion_failure == "operator_same"
                else "operator@example.com"
            )
            assert session.exec(select(CredentialSource)).one().account_id == (
                "operator@example.com" if promotion_failure == "operator_other" else "default"
            )
    engine.dispose()
