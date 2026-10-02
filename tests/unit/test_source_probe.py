"""The on-demand credential-source probe (#434) and the outcome rule it shares with failover."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import auth_failures
from app.services.collector_manager import CollectorManager
from app.services.smart_collector import SmartCollector
from app.services.source_probe import isolated_collector, probe_sources, source_outcome
from app.services.token_cache import TokenCache, _active_source

GOOD = {"service_name": "x", "remaining": "10", "data_source": "api"}
ERR_AUTH = {"service_name": "x", "remaining": "ERR", "error_type": "auth_failed"}
ERR_OTHER = {"service_name": "x", "remaining": "ERR", "error_type": "api_error"}


@pytest.mark.parametrize(
    ("result", "rejected", "empty_ok", "expected"),
    [
        ([GOOD], False, False, "healthy"),
        # An optional request 401s but quota was collected: keep the data, note the problem.
        ([GOOD], True, False, "degraded"),
        ([ERR_AUTH], False, False, "auth_failed"),
        ([ERR_OTHER], True, False, "auth_failed"),  # the response was rejected at HTTP level
        ([ERR_OTHER], False, False, "unavailable"),
        ([], False, False, "unavailable"),
        ([], True, False, "auth_failed"),
        ([], False, True, "healthy"),  # a collector that legitimately reports nothing
        ([GOOD, ERR_AUTH], False, False, "healthy"),  # usable cards win over a stray error card
    ],
)
def test_source_outcome_truth_table(result, rejected, empty_ok, expected):
    assert source_outcome(result, rejected, empty_ok) == expected


class _Stub:
    """A collector whose answer depends on which source the probe pinned."""

    PROVIDER_ID = "deepseek"
    REFRESHABLE = True
    successful_empty_result = False
    behaviours: dict[str, object] = {}
    seen: list[tuple[str, bool]] = []
    instances: list[_Stub] = []

    def __init__(self, account_id=None, account_label=None):
        self.account_id = account_id or "default"
        self.account_label = account_label
        self.credential_account_id = "default"
        self._user_strategies = None
        self.verified_identity = None
        self.verified_subject = None
        self.collect_calls = 0
        _Stub.instances.append(self)

    def apply_strategy_config(self, strategies):
        self._user_strategies = strategies

    async def collect(self, client):
        self.collect_calls += 1
        selection = _active_source.get()
        source_id = selection[2] if selection else "?"
        _Stub.seen.append((source_id, self.REFRESHABLE))
        behaviour = _Stub.behaviours.get(source_id, [GOOD])
        if isinstance(behaviour, Exception):
            raise behaviour
        if behaviour == "401":
            for hook in client.event_hooks["response"]:
                await hook(SimpleNamespace(status_code=401, request=None))
            # What a real collector's error handling does on a 401.
            auth_failures.mark(self.PROVIDER_ID, self.credential_account_id)
            return []
        return behaviour


def _candidate(source_id: str, priority: int = 0, **extra) -> dict:
    return {"source_id": source_id, "enabled": True, "priority": priority, **extra}


@pytest.fixture(autouse=True)
def _reset_stub():
    _Stub.behaviours, _Stub.seen, _Stub.instances = {}, [], []
    auth_failures.reset()
    yield
    auth_failures.reset()


@pytest.fixture(name="manager")
def manager_fixture(monkeypatch):
    m = CollectorManager()
    shared = _Stub(account_id="default")
    m.smart_collectors = {
        "deepseek:default": SmartCollector(collector=shared, collector_name="x", ttl=60)
    }
    m.shared_collector = shared  # for assertions
    m._sync_collectors = AsyncMock()
    # Every write a real collection makes must stay untouched.
    for name in ("_record_source_health", "_promote_source_identity", "_revoke_cookie_tag"):
        monkeypatch.setattr(m, name, Mock(), raising=False)
    monkeypatch.setattr("app.services.source_probe.token_cache", TokenCache())
    monkeypatch.setattr("app.services.collector_manager.token_cache", TokenCache())
    return m


def _serve(manager, candidates):
    manager._source_candidates = AsyncMock(return_value=candidates)


@pytest.mark.asyncio
async def test_each_source_is_probed_pinned_in_failover_order_without_touching_the_poller(manager):
    _Stub.behaviours = {"src:rejected": "401", "src:down": [ERR_OTHER]}
    _serve(
        manager,
        [_candidate("src:down", 2), _candidate("src:good", 1), _candidate("src:rejected", 0)],
    )

    results = await probe_sources(manager, "deepseek", "default")

    assert [(r["source_id"], r["outcome"]) for r in results] == [
        ("src:rejected", "auth_failed"),
        ("src:good", "healthy"),
        ("src:down", "unavailable"),
    ]
    assert results[0]["http_status"] == 401 and results[1]["cards"] == 1
    assert all(r["probed"] for r in results)
    # Each attempt was pinned to its own source, on a copy: the poller's collector is untouched.
    assert [s for s, _ in _Stub.seen] == ["src:rejected", "src:good", "src:down"]
    assert manager.shared_collector.collect_calls == 0
    # Nothing a real collection writes was written.
    manager._record_source_health.assert_not_called()
    manager._promote_source_identity.assert_not_called()
    manager._revoke_cookie_tag.assert_not_called()


@pytest.mark.asyncio
async def test_a_probe_never_refreshes_a_token(manager):
    _serve(manager, [_candidate("src:a")])
    await probe_sources(manager, "deepseek", "default")
    assert _Stub.seen == [("src:a", False)]  # REFRESHABLE forced off on the copy
    assert manager.shared_collector.REFRESHABLE is True


@pytest.mark.asyncio
async def test_sources_that_cannot_or_should_not_be_called_are_reported_not_probed(manager):
    manager._awaiting_machine_renewal = Mock(
        side_effect=lambda p, c, all_: c["source_id"] == "src:idle"
    )
    manager.set_credential_source_preferences("deepseek", "default", {"src:off": (False, 9)})
    _serve(
        manager,
        [
            _candidate("src:idle", 0),
            _candidate("src:ok", 1),
            _candidate("src:off", 9),
            _candidate("src:pending", 3, identity_pending=True),
        ],
    )

    results = {r["source_id"]: r for r in await probe_sources(manager, "deepseek", "default")}

    assert results["src:idle"] == {
        "source_id": "src:idle",
        "outcome": "waiting_on_machine",
        "probed": False,
    }
    assert results["src:off"]["outcome"] == "disabled" and not results["src:off"]["probed"]
    assert results["src:pending"]["outcome"] == "pending" and not results["src:pending"]["probed"]
    assert results["src:ok"]["outcome"] == "healthy"
    assert [s for s, _ in _Stub.seen] == ["src:ok"]  # nothing else touched the network


@pytest.mark.asyncio
async def test_a_rested_source_is_still_probed(manager):
    from datetime import UTC, datetime, timedelta

    manager._credential_source_state = {
        ("deepseek", "default"): {
            "src:rested": ("auth_failed", datetime.now(UTC) + timedelta(hours=3))
        }
    }
    _serve(manager, [_candidate("src:rested", 0), _candidate("src:good", 1)])
    results = await probe_sources(manager, "deepseek", "default")
    # Failover would skip the rested source (a working one is available); a probe is there to
    # show exactly that source's state, so it is probed too.
    assert [r["source_id"] for r in results] == ["src:rested", "src:good"]
    assert all(r["probed"] for r in results)


@pytest.mark.asyncio
async def test_a_crashing_collector_is_reported_with_the_message_redacted(manager):
    _Stub.behaviours = {
        "src:a": RuntimeError("boom Bearer abcdef0123456789abcdef0123456789")
    }  # pragma: allowlist secret
    _serve(manager, [_candidate("src:a")])

    (result,) = await probe_sources(manager, "deepseek", "default")

    assert result["outcome"] == "unavailable" and result["error_type"] == "RuntimeError"
    assert "abcdef0123456789abcdef0123456789" not in result["message"]  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_a_probe_never_flags_the_account_as_rejected(manager):
    _Stub.behaviours = {"src:rejected": "401"}
    _serve(manager, [_candidate("src:rejected")])
    await probe_sources(manager, "deepseek", "default")
    assert auth_failures.flagged_accounts("deepseek") == set()

    # ...and it never clears a flag a real collection set (the poller's own verdict).
    auth_failures.mark("deepseek", "default")
    await probe_sources(manager, "deepseek", "default")
    assert auth_failures.flagged_accounts("deepseek") == {"default"}


def test_isolated_collector_copies_configuration_but_shares_no_state():
    template = _Stub(account_id="alice@example.com", account_label="Alice")
    template.credential_account_id = "slot-1"
    template.apply_strategy_config({"web": {"enabled": False}})

    clone = isolated_collector(template)

    assert clone is not template
    assert (clone.account_id, clone.account_label) == ("alice@example.com", "Alice")
    assert clone.credential_account_id == "slot-1"
    assert clone._user_strategies == {"web": {"enabled": False}}
    clone.account_id = "changed-by-a-response"
    assert template.account_id == "alice@example.com"


@pytest.mark.asyncio
async def test_unknown_account_without_a_collector_returns_nothing(manager, monkeypatch):
    manager.smart_collectors = {}
    monkeypatch.setattr(manager, "_create_collector", Mock(return_value=None))
    assert await probe_sources(manager, "deepseek", "ghost") == []


@pytest.mark.asyncio
async def test_collectors_run_in_probe_mode_so_nothing_they_learn_is_remembered(manager):
    from app.services.probe_mode import is_probing

    modes: list[bool] = []
    original = _Stub.collect

    async def spying(self, client):
        modes.append(is_probing())
        return await original(self, client)

    _Stub.collect = spying
    try:
        _serve(manager, [_candidate("src:a"), _candidate("src:b", 1)])
        await probe_sources(manager, "deepseek", "default")
    finally:
        _Stub.collect = original
    assert modes == [True, True]
    assert is_probing() is False  # the switch never leaks out of the probe


@pytest.mark.asyncio
async def test_a_rejection_that_escapes_as_an_exception_still_reads_as_rejected(manager):
    class RejectedError(Exception):
        pass

    async def collect(self, client):
        for hook in client.event_hooks["response"]:
            await hook(SimpleNamespace(status_code=403, request=None))
        raise RejectedError("403 Forbidden")

    original = _Stub.collect
    _Stub.collect = collect
    try:
        _serve(manager, [_candidate("src:a")])
        (result,) = await probe_sources(manager, "deepseek", "default")
    finally:
        _Stub.collect = original
    assert result["outcome"] == "auth_failed" and result["http_status"] == 403


@pytest.mark.asyncio
async def test_many_sources_are_all_probed_and_reported_in_failover_order(manager):
    candidates = [_candidate(f"src:{n}", n) for n in range(9)]
    _serve(manager, candidates)
    results = await probe_sources(manager, "deepseek", "default")
    assert [r["source_id"] for r in results] == [f"src:{n}" for n in range(9)]
    assert all(r["outcome"] == "healthy" for r in results)


@pytest.mark.asyncio
async def test_token_cache_writes_and_error_flags_are_suppressed_only_while_probing():
    from app.services.probe_mode import probing

    cache = TokenCache()
    with probing():
        await cache.store(
            "deepseek", {"api_key": "k"}, account_id="alice@example.com"
        )  # pragma: allowlist secret
        auth_failures.mark("deepseek", "alice@example.com")
    assert await cache.get_token("deepseek", "api_key", account_id="alice@example.com") is None
    assert auth_failures.flagged_accounts("deepseek") == set()

    await cache.store(
        "deepseek", {"api_key": "k"}, account_id="alice@example.com"
    )  # pragma: allowlist secret
    auth_failures.mark("deepseek", "alice@example.com")
    assert await cache.get_token("deepseek", "api_key", account_id="alice@example.com") == "k"
    assert auth_failures.flagged_accounts("deepseek") == {"alice@example.com"}


def test_provider_error_events_are_not_logged_by_a_probe(monkeypatch):
    import httpx

    from app.services.collectors.deepseek import DeepSeekCollector
    from app.services.probe_mode import probing

    recorded = Mock()
    monkeypatch.setattr("app.services.error_events.record_provider_error", recorded)
    monkeypatch.setattr("app.core.db.engine", Mock())
    collector = DeepSeekCollector()
    failure = httpx.HTTPStatusError(
        "boom", request=httpx.Request("GET", "https://x"), response=httpx.Response(429)
    )

    with probing():
        collector._record_strategy_error(failure)
    recorded.assert_not_called()

    # Outside a probe the same failure is still recorded (the guard is the only difference).
    collector._record_strategy_error(failure)
    recorded.assert_called_once()
