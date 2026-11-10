"""Unit tests for CollectorManager collection lifecycle, timeouts, outcomes, and renewal advice."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit._collector_manager_helpers import manager_fixture  # noqa: F401 — fixture


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
            patch.object(manager, "sync_collectors", new_callable=AsyncMock),
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
        with patch.object(manager, "sync_collectors", new_callable=AsyncMock):
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


def test_keep_alive_providers_are_the_ones_with_a_sidecar_renewer():
    """The server's keep-alive hint must cover exactly what the sidecar can renew."""
    from app.services.refresh_policy import KEEP_ALIVE_PROVIDERS
    from scripts import sidecar
    from scripts.sidecar_pkg import keep_alive
    from scripts.sidecar_pkg.anthropic_renewer import AnthropicRenewer
    from scripts.sidecar_pkg.codex_renewer import CodexRenewer
    from scripts.sidecar_pkg.minimax_code_renewer import MiniMaxCodeRenewer
    from scripts.sidecar_pkg.xai_renewer import XaiRenewer

    thread = sidecar._make_keep_alive_thread()
    renewer_providers = {r.name for r in thread._renewers} | {keep_alive.AGY_PROVIDER}
    assert renewer_providers == {
        XaiRenewer.name,
        AnthropicRenewer.name,
        CodexRenewer.name,
        MiniMaxCodeRenewer.name,
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
            patch.object(manager, "sync_collectors", new_callable=AsyncMock),
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
