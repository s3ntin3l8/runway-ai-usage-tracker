"""Unit tests for CollectorManager default collector source sweep across resolved accounts."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.collector_manager import CollectorManager
from tests.unit._collector_manager_helpers import manager_fixture  # noqa: F401 — fixture


class TestDefaultCollectorSourceSweep:
    """The default collector must find bundles filed under a resolved identity.

    Sidecar ingest keys source bundles by the account it proved (the email from
    userinfo), never by ``default``. Without a sweep, ``source_candidates``
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
        monkeypatch.setattr(manager, "promote_source_identity", promote)
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

        ordered = manager.ordered_candidates("antigravity", "default", swept)

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
        ordered = manager.ordered_candidates("anthropic", "default", candidates)
        assert [c["source_id"] for c in ordered] == ["src:good", "src:other"]

        # Rest is over: it is due, so it is tried in its normal place. Demoting it instead
        # would starve it — failover stops at the first source that works.
        manager._credential_source_state = {
            ("anthropic", "default"): {
                "src:revoked": ("auth_failed", datetime.now(UTC) - timedelta(seconds=1))
            }
        }
        ordered = manager.ordered_candidates("anthropic", "default", candidates)
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
        ordered = manager.ordered_candidates("anthropic", "default", candidates)
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

        candidates = await CollectorManager.source_candidates("antigravity", "default", False)

        assert [candidate["source_id"] for candidate in candidates] == ["src:dup", "src:swept"]
