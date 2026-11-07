"""Unit tests for per-account event extraction loop and run_collection integration.

Covers:
- Iterating per-account event extraction in ``_extract_events_for_provider``.
- Tolerating partial extractor failures across accounts.
- Local identity intersect in ``run_collection`` (one account per sidecar host).
- Rejecting cross-host fallback when local identity does not match server accounts.
- Lazy initialization of CredentialCache via ``_get_credential_cache()``.
- Outage tolerance preserving cached identities during collection cycle.
- Real path-list extractors through the dispatch table (anthropic, chatgpt, gemini).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.unit._sidecar_credentials_helpers import (  # noqa: F401
    approved_keychain_readers,
    isolate_host_credentials,
)


def test_extract_events_for_provider_calls_extractor_per_account(monkeypatch):
    """``_extract_events_for_provider`` runs the extractor once per
    account and appends all events to ``out_events``."""
    import scripts.sidecar as sc

    calls: list[str] = []

    class _FakeEvt:
        def __init__(self, eid: str) -> None:
            self.event_id = eid

        def model_dump(self, mode: str = "python") -> dict:
            return {"event_id": self.event_id}

    def _fake_extractor(
        account_id: str,
        watermark,
        bootstrap_days: int,
        *,
        canonical_hints: dict | None = None,  # noqa: ARG001 — accepted for signature parity (PR #318)
    ) -> list:
        calls.append(account_id)
        return [_FakeEvt(f"evt-{account_id}-1"), _FakeEvt(f"evt-{account_id}-2")]

    # Replace all three extractor factories so any provider the dispatch
    # table picks yields our stub.
    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: _fake_extractor)

    out: list[dict] = []
    sc._extract_events_for_provider(
        "anthropic",
        ["default", "alice@example.com"],
        watermark=MagicMock(last_pushed=MagicMock(return_value=None)),
        bootstrap_days=90,
        out_events=out,
    )

    # The extractor was called once per account, in order.
    assert calls == ["default", "alice@example.com"]
    # Both events from both accounts were appended.
    assert [e["event_id"] for e in out] == [
        "evt-default-1",
        "evt-default-2",
        "evt-alice@example.com-1",
        "evt-alice@example.com-2",
    ]


def test_extract_events_for_provider_continues_on_partial_failure(monkeypatch):
    """If one account raises, the others still run (issue #272 spec:
    "Handle partial failure: if one account's credentials fail to fetch
    / decrypt, others continue.")."""
    call_log: list[str] = []

    def _flaky(
        account_id: str,
        watermark,
        bootstrap_days: int,
        *,
        canonical_hints: dict | None = None,  # noqa: ARG001 — accepted for signature parity (PR #318)
    ) -> list:
        call_log.append(account_id)
        if account_id == "broken":
            raise RuntimeError("redisconnect")
        return []

    with (
        patch.object(
            sc_mod := __import__("scripts.sidecar", fromlist=["*"]),
            "_make_account_extractor",
            return_value=_flaky,
        ),
        patch.object(sc_mod, "_make_account_extractor_opencode", return_value=_flaky),
        patch.object(sc_mod, "_make_account_extractor_antigravity", return_value=_flaky),
    ):
        out: list[dict] = []
        sc_mod._extract_events_for_provider(
            "anthropic",
            ["good", "broken", "also_good"],
            watermark=MagicMock(last_pushed=MagicMock(return_value=None)),
            bootstrap_days=90,
            out_events=out,
        )
    assert call_log == ["good", "broken", "also_good"]


def test_extract_events_no_accounts_no_calls(monkeypatch):
    """Empty account list → no extractor calls, no errors."""
    out: list[dict] = []
    sc_mod = __import__("scripts.sidecar", fromlist=["*"])
    sc_mod._extract_events_for_provider(
        "anthropic",
        [],
        watermark=MagicMock(),
        bootstrap_days=90,
        out_events=out,
    )
    assert out == []


def test_run_collection_heartbeat_discovers_credentials_without_polling(monkeypatch):
    """An empty quota poll still discovers token cards from providers."""
    import scripts.sidecar as sc

    class Cache:
        def is_fresh(self):
            return True

        def provider_accounts(self):
            return {}

        def provider_tag_hints(self):
            return {}

    quota = {"service_name": "OpenRouter local quota", "remaining": "$2", "unit": "USD"}
    monkeypatch.setattr(sc, "_CREDENTIAL_CACHE", Cache())
    monkeypatch.setitem(sc._CREDENTIAL_DISCOVERY_STATE, "last_scanned_at", {})
    monkeypatch.setattr(sc, "_EVENT_PROVIDERS", frozenset())
    # Fresh GitHub runners can have a monotonic clock below the ten-minute
    # interval, so zero must be treated as "never scanned" explicitly.
    now = [42.0]
    monkeypatch.setattr(sc.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        sc,
        "__REGISTRY__",
        {"providers": {"openrouter": {"name": "OpenRouter"}, "openai": {"name": "OpenAI"}}},
    )
    calls: list[str] = []

    def collect_provider(provider_id, *args, **kwargs):
        calls.append(provider_id)
        card = {
            "service_name": provider_id,
            "remaining": "Token",
            "unit": "api_key",
            "metadata": {"provider_id": provider_id, "api_key": "sk-test"},
        }
        return [card, quota], []

    monkeypatch.setattr(
        sc.GenericCollector,
        "collect_provider",
        staticmethod(collect_provider),
    )
    monkeypatch.setattr(sc, "_post_credential_manifest", lambda **kwargs: None)

    result = sc.run_collection(config={}, providers=[])

    assert [card["metadata"]["provider_id"] for card in result.metrics] == [
        "openrouter",
        "openai",
    ]
    assert result.events == []
    assert result.error_count == 0
    assert calls == ["openrouter", "openai"]

    now[0] = 43.0
    # A newly surfaced provider should be scanned without rescanning the
    # providers whose credential files were just checked.
    sc._CREDENTIAL_DISCOVERY_STATE["last_scanned_at"].pop("openai")
    result = sc.run_collection(config={}, providers=[])
    assert [card["metadata"]["provider_id"] for card in result.metrics] == ["openai"]
    assert result.events == []
    assert result.error_count == 0
    assert calls == ["openrouter", "openai", "openai"]


def test_run_collection_iterates_one_account_matching_local_identity(monkeypatch, tmp_path):
    """End-to-end: ``run_collection`` reads the cached per-account list,
    intersects it with the locally-discovered ``account_id``, and emits
    events stamped with the matching identity.

    Patches ``_CREDENTIAL_CACHE`` directly with two server-side accounts;
    stubs ``_LEGACY_EVENT_ACCOUNT_DISCOVERY`` to return one of them as the
    local identity; verifies the extractor is called only for that one
    (the other belongs to a different sidecar host, so we don't re-stamp
    the same events under a different ``account_id``).
    """
    import scripts.sidecar as sc

    monkeypatch.setattr(sc.Path, "home", lambda: tmp_path)

    # Pre-populate the cache directly via the module-global (a wrapper
    # test below uses the real ``_get_credential_cache()`` path).
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace(
        accounts={"anthropic": ["default", "alice@example.com"]},
        tokens={
            ("anthropic", "default"): "tok-default",
            ("anthropic", "alice@example.com"): "tok-alice",
        },
    )
    monkeypatch.setattr(sc, "_CREDENTIAL_CACHE", cache)

    # Local discovery returns ``"alice@example.com"`` for this host. The
    # server has both accounts, but this sidecar should only iterate its
    # own.
    monkeypatch.setattr(
        sc,
        "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
        {
            "anthropic": lambda: "alice@example.com",
        },
    )

    # Stub the per-account dispatcher to record calls.
    emitted: list[dict] = []

    class _Evt:
        def __init__(self, eid: str) -> None:
            self.event_id = eid

        def model_dump(self, mode: str = "python") -> dict:
            return {"provider_id": "anthropic", "event_id": self.event_id}

    def _fake_extractor(
        account_id: str,
        watermark,
        bootstrap_days: int,
        *,
        canonical_hints: dict | None = None,  # noqa: ARG001 — accepted for signature parity (PR #318)
    ) -> list:
        emitted.append({"account_id": account_id, "event_id": f"evt-{account_id}"})
        return [_Evt(f"evt-{account_id}")]

    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: _fake_extractor)

    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

    config: dict = {"api_url": "http://unused", "api_key": "secret"}

    result = sc.run_collection(config=config, providers=["anthropic"])

    assert result.error_count == 0
    assert result.metrics == []
    events = result.events
    # Exactly one account iterated → one event emitted, stamped with the
    # matched account_id. The other server-side account is ignored.
    assert len(events) == 1
    assert events[0]["event_id"] == "evt-alice@example.com"
    assert [e["account_id"] for e in emitted] == ["alice@example.com"]


def test_run_collection_stamps_local_identity_when_no_server_match(monkeypatch, tmp_path):
    """If local discovery returns ``"default"`` but the server has only
    other hosts' accounts for this provider (e.g. ``alice@example.com``
    is registered but lives on a different sidecar), we MUST NOT pick
    someone else's account (PR #283 review).

    Falling back to ``provider_accounts[0]`` would attribute this
    sidecar's events to another user's account — even more destructive
    than the original leak from #272. Instead we stamp with the
    locally-discovered id (``"default"``) and log a debug line about
    the mismatch.
    """
    import scripts.sidecar as sc

    monkeypatch.setattr(sc.Path, "home", lambda: tmp_path)
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace(
        accounts={"anthropic": ["alice@example.com"]},
        tokens={("anthropic", "alice@example.com"): "tok-alice"},
    )
    monkeypatch.setattr(sc, "_CREDENTIAL_CACHE", cache)

    monkeypatch.setattr(
        sc,
        "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
        {"anthropic": lambda: "default"},  # local has no email
    )

    emitted: list[dict] = []

    class _Evt:
        def __init__(self, eid: str) -> None:
            self.event_id = eid

        def model_dump(self, mode: str = "python") -> dict:
            return {"provider_id": "anthropic", "event_id": self.event_id}

    def _fake_extractor(
        account_id: str,
        watermark,
        bootstrap_days: int,
        *,
        canonical_hints: dict | None = None,  # noqa: ARG001 — accepted for signature parity (PR #318)
    ) -> list:
        emitted.append(account_id)
        return [_Evt(f"evt-{account_id}")]

    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

    config: dict = {"api_url": "http://unused", "api_key": "secret"}

    result = sc.run_collection(config=config, providers=["anthropic"])
    assert result.error_count == 0
    # Cross-host fallback was rejected: we stamped with the local
    # identity ("default"), NOT with server's "alice@example.com" (which
    # belongs to another sidecar).
    assert emitted == ["default"]


def test_run_collection_uses_get_credential_cache_factory(monkeypatch, tmp_path):
    """Production-wiring regression: ``run_collection`` must lazy-init the
    credential cache via ``_get_credential_cache()`` — never access the
    bare ``_CREDENTIAL_CACHE`` module global directly (PR #283 review).

    We can't just assert ``errors == 0`` because the wide ``except``
    around the cache call would swallow a regression's ``AttributeError``
    silently at DEBUG. Instead we spy on the factory itself: a regression
    that bypasses the lazy init and reads ``sc._CREDENTIAL_CACHE``
    directly will fail this test before any exception can be swallowed.
    """
    import scripts.sidecar as sc

    # Reset the lazy singleton to simulate a cold start.
    monkeypatch.setattr(sc, "_CREDENTIAL_CACHE", None)

    # Spy: every call to the factory records an entry. We return a fresh
    # empty cache so the run completes without touching the network.
    from scripts.sidecar_pkg.credentials import CredentialCache

    real_cache = CredentialCache()
    real_cache.replace(accounts={}, tokens={})
    real_cache._identities_fetched_at = 1.0  # mark "fresh" so we skip the HTTP fetch

    calls: list[Any] = []

    def _spy_factory() -> Any:
        calls.append(1)
        return real_cache

    monkeypatch.setattr(sc, "_get_credential_cache", _spy_factory)

    # Stub the legacy discovery to return "default".
    monkeypatch.setattr(
        sc,
        "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
        {"anthropic": lambda: "default"},
    )
    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: lambda *x, **k: [])
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: lambda *x, **k: [])
    monkeypatch.setattr(
        sc, "_make_account_extractor_antigravity", lambda *a, **kw: lambda *x, **k: []
    )
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

    result = sc.run_collection(config={}, providers=["anthropic"])
    assert result.error_count == 0
    # The production code MUST go through the factory; reaching for the
    # module-global directly would skip ``_get_credential_cache()`` and
    # leave ``calls`` empty.
    assert calls, (
        "run_collection did not invoke _get_credential_cache() — "
        "the bare module global was reached for directly, regressing the "
        "production wiring fix from PR #283"
    )


def test_run_collection_swallows_lazy_init_failure(monkeypatch):
    """If ``_get_credential_cache()`` itself raises (lazy import failure
    on a frozen binary, missing dependency, anything), ``run_collection``
    keeps going and the legacy single-account path runs (PR #283 review).

    We can't reach for ``sc._CREDENTIAL_CACHE`` to detect the failure
    because the lazy init is the whole point — so we assert that the
    legacy event extractor IS called even when the factory raises.
    """
    import scripts.sidecar as sc

    def _boom() -> Any:
        raise ImportError("simulated missing credentials module")

    monkeypatch.setattr(sc, "_get_credential_cache", _boom)

    emitted: list[str] = []

    monkeypatch.setattr(
        sc,
        "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
        {"anthropic": lambda: "default"},
    )

    def _fake_extractor(
        account_id: str,
        watermark,
        bootstrap_days: int,
        *,
        canonical_hints: dict | None = None,  # noqa: ARG001 — accepted for signature parity (PR #318)
    ) -> list:
        emitted.append(account_id)
        return []

    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

    result = sc.run_collection(config={}, providers=["anthropic"])
    assert result.error_count == 0
    # Even with the cache factory blowing up, the legacy path still ran.
    assert emitted == ["default"]


def test_run_collection_falls_back_to_legacy_when_no_server_accounts(monkeypatch):
    """When the server's /fleet/config has no per-account entries for
    the provider, ``run_collection`` falls back to the legacy
    single-account path (local discovery + ``account_id="default"``)."""
    import scripts.sidecar as sc
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace(accounts={}, tokens={})
    monkeypatch.setattr(sc, "_CREDENTIAL_CACHE", cache)

    # Stub the legacy discovery to return "default".
    monkeypatch.setattr(
        sc,
        "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
        {
            "anthropic": lambda: "default",
        },
    )

    emitted: list[str] = []

    def _fake_extractor(
        account_id: str,
        watermark,
        bootstrap_days: int,
        *,
        canonical_hints: dict | None = None,  # noqa: ARG001 — accepted for signature parity (PR #318)
    ) -> list:
        emitted.append(account_id)
        return []

    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: _fake_extractor)

    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

    sc.run_collection(config={}, providers=["anthropic"])

    # Single fallback call with the legacy "default" identity.
    assert emitted == ["default"]


def test_run_collection_keeps_prior_cache_when_fetch_identity_hints_returns_none(
    monkeypatch,
):
    """When ``fetch_identity_hints`` returns ``None`` (outage) and the
    cache is stale, the ``if fetched is not None`` guard skips the
    ``cache.replace`` call. The prior snapshot survives and the next
    cycle retries — proving the outage-tolerant contract end-to-end
    (PR #283 round-4 review).

    Mutation check from the reviewer: ``cache.replace(accounts=fetched or {})``
    would pass every other test in this file but would clobber the cache
    with an empty identity view, suppressing all per-account discovery
    for the remainder of the 10-min TTL. The guard exists to keep prior
    state intact on outage.
    """
    import scripts.sidecar as sc
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace(
        accounts={"anthropic": ["default", "alice@example.com"]},
        tokens={
            ("anthropic", "default"): "tok-default",
            ("anthropic", "alice@example.com"): "tok-alice",
        },
    )
    # Mark as stale so the next cycle attempts a fetch.
    cache._identities_fetched_at = 0.0
    initial_tokens_at = cache._identities_fetched_at
    preserved_snapshot = cache.provider_accounts()

    monkeypatch.setattr(sc, "_CREDENTIAL_CACHE", cache)

    # Outage: fetch_identity_hints returns None.
    monkeypatch.setattr(
        "scripts.sidecar_pkg.credentials.fetch_identity_hints",
        lambda api_url: None,
    )

    monkeypatch.setattr(
        sc,
        "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
        {"anthropic": lambda: "default"},
    )
    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: lambda *x, **k: [])
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: lambda *x, **k: [])
    monkeypatch.setattr(
        sc, "_make_account_extractor_antigravity", lambda *a, **kw: lambda *x, **k: []
    )
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

    config = {"api_url": "https://api.example.com"}
    result = sc.run_collection(config=config, providers=["anthropic"])

    assert result.error_count == 0
    # The prior snapshot survived: the ``if fetched is not None`` guard
    # skipped ``cache.replace(accounts=None)``. A regression that uses
    # ``cache.replace(accounts=fetched or {})`` would clobber this with
    # an empty dict.
    assert cache.provider_accounts() == preserved_snapshot, (
        "Outage must not clobber the cache — the if fetched is not None "
        "guard exists to keep prior state intact so the next cycle retries."
    )
    # And the freshness sentinel still says "stale", so the next cycle
    # will retry (the snapshot is preserved, not the freshness claim).
    assert cache.is_fresh() is False
    assert cache._identities_fetched_at == initial_tokens_at


# ---------------------------------------------------------------------------
# scripts/sidecar.py — real path-list extractors through the dispatch table
# (issue #320: ``_make_account_extractor`` fed parsers one ``Path`` at a
# time while they expect ``list[Path]``, so anthropic/chatgpt/gemini
# extraction raised ``TypeError`` every cycle. The tests above stub the
# factory, which is how the regression slipped through — these run the
# real parser against the checked-in fixtures.)
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.mark.parametrize(
    ("provider_id", "discover_fn", "fixture_name"),
    [
        ("anthropic", "_discover_anthropic_log_paths", "anthropic-sample.jsonl"),
        ("chatgpt", "_discover_codex_log_paths", "chatgpt-sample.jsonl"),
        ("gemini", "_discover_gemini_log_paths", "gemini-sample.jsonl"),
    ],
)
def test_extract_events_for_provider_runs_real_path_list_parser(
    monkeypatch, caplog, provider_id, discover_fn, fixture_name
):
    import datetime as _dt

    import scripts.sidecar as sc

    fixture = _FIXTURES_DIR / fixture_name
    # Two paths — the discovery helpers return a list, and the parser must
    # receive that list (not each element) exactly once.
    monkeypatch.setattr(sc, discover_fn, lambda: [fixture, fixture])

    watermark = MagicMock(
        last_pushed=MagicMock(return_value=_dt.datetime(2020, 1, 1, tzinfo=_dt.UTC))
    )
    out: list[dict] = []
    with caplog.at_level("WARNING"):
        sc._extract_events_for_provider(
            provider_id,
            ["u@example.com"],
            watermark=watermark,
            bootstrap_days=90,
            out_events=out,
        )

    assert "event extraction error" not in caplog.text
    assert out, f"{provider_id}: expected events from {fixture_name}"
    assert all(e["provider_id"] == provider_id for e in out)
    assert all(e["account_id"] == "u@example.com" for e in out)
    # The same file listed twice must not double-count.
    event_ids = [e["event_id"] for e in out]
    assert len(event_ids) == len(set(event_ids))
