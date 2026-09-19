"""Tests for the sidecar identity cache (issue #272, deferred redeem).

Covers:
- ``scripts/sidecar_pkg/credentials.py``: ``fetch_identity_hints`` and
  ``fetch_credential_tokens`` HTTP plumbing, plus the ``CredentialCache``
  staleness / replacement / decoupled-identity semantics.
- The per-account event-extraction loop wired into ``run_collection`` —
  the dispatch table, the local-identity intersect (one account per
  host), the legacy fallback when the server has no per-account config,
  the partial-failure tolerance.

The redeem side (``redeem_credential`` / HMAC headers / decrypted body)
intentionally is NOT exercised here — that endpoint ships with the first
production caller in the follow-up PR; today there is none, so the
implementation has been deferred to keep public surface tight.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# scripts/sidecar_pkg/credentials — token fetch + redeem
# ---------------------------------------------------------------------------


def _ok_response(status: int = 200, body: bytes | None = None) -> MagicMock:
    resp = MagicMock()
    resp.getcode = MagicMock(return_value=status)
    if status == 200 and body is not None:
        resp.read = MagicMock(return_value=body)
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
    else:
        # For error path, .read() raises HTTPError
        from urllib import error

        resp.read = MagicMock(
            side_effect=error.HTTPError(
                url="x", code=status, msg="err", hdrs={}, fp=MagicMock(read=lambda: b"")
            )
        )
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestFetchIdentityHints:
    """``fetch_identity_hints`` is the identity-only view decoupled from
    credential-token issuance. Rows without a token still contribute their
    ``account_id`` here — gating identity discovery on token presence
    would silently no-op the #272 attribution fix in any configuration
    where the row has no credentials or INGEST_API_KEY is empty
    (PR #283 review)."""

    def test_returns_empty_when_urlopen_raises(self):
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        with patch("scripts.sidecar_pkg.tls.build_context", return_value=None):
            with patch("urllib.request.urlopen", side_effect=TimeoutError("nope")):
                assert fetch_identity_hints("https://api.example.com") == {}

    def test_returns_empty_on_non_200(self):
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        with patch("scripts.sidecar_pkg.tls.build_context", return_value=None):
            with patch("urllib.request.urlopen") as mock_urlopen:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(
                    return_value=MagicMock(getcode=MagicMock(return_value=500))
                )
                ctx.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = ctx
                assert fetch_identity_hints("https://api.example.com") == {}

    def test_parses_per_account_ids_independent_of_token(self):
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        payload = json.dumps(
            {
                "config": {
                    "providers": {
                        "anthropic": {
                            "accounts": [
                                {"account_id": "default", "credential_token": "tok-default"},
                                {
                                    "account_id": "alice@example.com",
                                    "credential_token": "tok-alice",
                                },
                                # Token-less row — must STILL appear in the
                                # identity hints.
                                {"account_id": "bob@example.com"},
                            ]
                        },
                        "chatgpt": {
                            "accounts": [
                                {"account_id": "default", "credential_token": "tok-chatgpt"}
                            ]
                        },
                        "empty": {"accounts": []},
                    }
                }
            }
        ).encode()
        with patch("scripts.sidecar_pkg.tls.build_context", return_value=None):
            with patch("urllib.request.urlopen") as mock_urlopen:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(
                    return_value=MagicMock(
                        getcode=MagicMock(return_value=200), read=MagicMock(return_value=payload)
                    )
                )
                ctx.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = ctx
                result = fetch_identity_hints("https://api.example.com")

        assert result == {
            "anthropic": ["default", "alice@example.com", "bob@example.com"],
            "chatgpt": ["default"],
        }
        # Provider with no accounts is absent.
        assert "empty" not in result


class TestFetchCredentialTokens:
    """``fetch_credential_tokens`` is the supplementary token map. It's a
    subset of the identity hints — rows where the server didn't issue a
    token (no credentials, or empty INGEST_API_KEY) are simply absent."""

    def test_parses_per_account_tokens(self):
        from scripts.sidecar_pkg.credentials import fetch_credential_tokens

        payload = json.dumps(
            {
                "config": {
                    "providers": {
                        "anthropic": {
                            "accounts": [
                                {"account_id": "default", "credential_token": "tok-default"},
                                {
                                    "account_id": "alice@example.com",
                                    "credential_token": "tok-alice",
                                },
                                # Row without a token — dropped here (the
                                # identity view keeps it; this view is the
                                # subset of issued tokens only).
                                {"account_id": "bob@example.com"},
                            ]
                        },
                        "chatgpt": {
                            "accounts": [
                                {"account_id": "default", "credential_token": "tok-chatgpt"}
                            ]
                        },
                    }
                }
            }
        ).encode()
        with patch("scripts.sidecar_pkg.tls.build_context", return_value=None):
            with patch("urllib.request.urlopen") as mock_urlopen:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(
                    return_value=MagicMock(
                        getcode=MagicMock(return_value=200), read=MagicMock(return_value=payload)
                    )
                )
                ctx.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = ctx
                result = fetch_credential_tokens("https://api.example.com")

        # Row without a token is dropped.
        assert result == {
            ("anthropic", "default"): "tok-default",
            ("anthropic", "alice@example.com"): "tok-alice",
            ("chatgpt", "default"): "tok-chatgpt",
        }


class TestCredentialCache:
    def test_provider_accounts_includes_rows_without_tokens(self):
        """The cache's provider_accounts() returns the identity view —
        every server-known row regardless of whether a token was issued.
        This is the core of the #272 fix (PR #283 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        # Identity map populated; token map deliberately empty (simulates
        # INGEST_API_KEY-empty config where no tokens are issued).
        cache.replace(
            accounts={
                "anthropic": ["default", "alice@example.com"],
                "chatgpt": ["default"],
            },
        )
        # provider_accounts still works.
        result = cache.provider_accounts()
        assert result == {
            "anthropic": ["default", "alice@example.com"],
            "chatgpt": ["default"],
        }
        # And the token map is empty — proves identity was decoupled.
        assert cache.tokens == {}

    def test_replace_replaces_prior_state(self):
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(accounts={"anthropic": ["default", "alice@example.com"]})
        assert cache.provider_accounts() == {"anthropic": ["default", "alice@example.com"]}
        # Re-replacing drops the prior set.
        cache.replace(accounts={"anthropic": ["bob@example.com"]})
        assert cache.provider_accounts() == {"anthropic": ["bob@example.com"]}

    def test_provider_accounts_order_is_arbitrary(self):
        """Explicit guardrail: the order mirrors JSON serialization and
        must not be relied on for positional lookup. Callers that pick
        ``provider_accounts[provider_id][0]`` will fail (PR #283 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(accounts={"anthropic": ["alice@example.com", "default"]})
        # The order is whatever the server returned; we don't reorder.
        accounts = cache.provider_accounts()["anthropic"]
        assert accounts == ["alice@example.com", "default"]
        # And consumers (run_collection) must use ``in`` checks, not
        # ``[0]`` indexing. This test simply pins the current behavior
        # so a future "sort for determinism" change is intentional.

    def test_is_fresh(self):
        import time as _t

        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache(ttl_seconds=60)
        # No fetch yet → not fresh
        assert not cache.is_fresh()
        cache.replace(accounts={})
        baseline = _t.time()
        # Fresh immediately after fetch (delta 0 < 60)
        assert cache.is_fresh(now=baseline)
        # Stale after TTL
        assert not cache.is_fresh(now=baseline + 61)


# ---------------------------------------------------------------------------
# scripts/sidecar.py — per-account event extraction loop
# ---------------------------------------------------------------------------


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

    def _fake_extractor(account_id: str, watermark, bootstrap_days: int) -> list:
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

    def _flaky(account_id: str, watermark, bootstrap_days: int) -> list:
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
            return {"event_id": self.event_id}

    def _fake_extractor(account_id: str, watermark, bootstrap_days: int) -> list:
        emitted.append({"account_id": account_id, "event_id": f"evt-{account_id}"})
        return [_Evt(f"evt-{account_id}")]

    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: _fake_extractor)

    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: [])

    config: dict = {"api_url": "http://unused", "api_key": "secret"}

    metrics, events, errors = sc.run_collection(config=config, providers=["anthropic"])

    assert errors == 0
    assert metrics == []
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
            return {"event_id": self.event_id}

    def _fake_extractor(account_id: str, watermark, bootstrap_days: int) -> list:
        emitted.append(account_id)
        return [_Evt(f"evt-{account_id}")]

    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: [])

    config: dict = {"api_url": "http://unused", "api_key": "secret"}

    _, _, errors = sc.run_collection(config=config, providers=["anthropic"])
    assert errors == 0
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
    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: lambda *x: [])
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: lambda *x: [])
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: lambda *x: [])
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: [])

    _, _, errors = sc.run_collection(config={}, providers=["anthropic"])
    assert errors == 0
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

    def _fake_extractor(account_id: str, watermark, bootstrap_days: int) -> list:
        emitted.append(account_id)
        return []

    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: [])

    _, _, errors = sc.run_collection(config={}, providers=["anthropic"])
    assert errors == 0
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

    def _fake_extractor(account_id: str, watermark, bootstrap_days: int) -> list:
        emitted.append(account_id)
        return []

    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: _fake_extractor)
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: _fake_extractor)

    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: [])

    sc.run_collection(config={}, providers=["anthropic"])

    # Single fallback call with the legacy "default" identity.
    assert emitted == ["default"]
