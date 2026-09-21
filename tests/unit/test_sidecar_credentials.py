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
    (PR #283 review).

    The function returns ``None`` on fetch failures (outage) to distinguish
    that from a successful empty response (PR #283 round-3 review).
    """

    def test_returns_none_when_urlopen_raises(self):
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        with patch("scripts.sidecar_pkg.tls.build_context", return_value=None):
            with patch("urllib.request.urlopen", side_effect=TimeoutError("nope")):
                result = fetch_identity_hints("https://api.example.com")
                assert result is None

    def test_returns_none_on_non_200(self):
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        with patch("scripts.sidecar_pkg.tls.build_context", return_value=None):
            with patch("urllib.request.urlopen") as mock_urlopen:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(
                    return_value=MagicMock(getcode=MagicMock(return_value=500))
                )
                ctx.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = ctx
                result = fetch_identity_hints("https://api.example.com")
                assert result is None

    def test_parses_per_account_ids_independent_of_token(self):
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        payload = json.dumps(
            {
                "config": {
                    "providers": {
                        "anthropic": {
                            "accounts": [
                                {
                                    "account_id": "default",
                                    "credential_token": "tok-default",
                                    "enabled": True,
                                },
                                {
                                    "account_id": "alice@example.com",
                                    "credential_token": "tok-alice",
                                    "enabled": True,
                                },
                                # Token-less row — must STILL appear in the
                                # identity hints.
                                {"account_id": "bob@example.com", "enabled": True},
                            ]
                        },
                        "chatgpt": {
                            "accounts": [
                                {
                                    "account_id": "default",
                                    "credential_token": "tok-chatgpt",
                                    "enabled": True,
                                }
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

    def test_skips_disabled_rows(self):
        """Disabled rows (the per-account enable toggle) must NOT appear in
        the identity hints, regardless of credential presence (PR #283
        round-3 review)."""
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        payload = json.dumps(
            {
                "config": {
                    "providers": {
                        "anthropic": {
                            "accounts": [
                                {
                                    "account_id": "default",
                                    "credential_token": "tok-default",
                                    "enabled": True,
                                },
                                {
                                    "account_id": "alice@example.com",
                                    "credential_token": "tok-alice",
                                    "enabled": False,
                                },
                            ]
                        }
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

        # Disabled row is filtered out.
        assert result == {"anthropic": ["default"]}


class TestFetchCredentialTokens:
    """``fetch_credential_tokens`` is the supplementary token map. It's a
    subset of the identity hints — rows where the server didn't issue a
    token (no credentials, or empty INGEST_API_KEY) are simply absent.

    Like :func:`fetch_identity_hints`, returns ``None`` on fetch failure
    (PR #283 round-3 review).
    """

    def test_parses_per_account_tokens(self):
        from scripts.sidecar_pkg.credentials import fetch_credential_tokens

        payload = json.dumps(
            {
                "config": {
                    "providers": {
                        "anthropic": {
                            "accounts": [
                                {
                                    "account_id": "default",
                                    "credential_token": "tok-default",
                                    "enabled": True,
                                },
                                {
                                    "account_id": "alice@example.com",
                                    "credential_token": "tok-alice",
                                    "enabled": True,
                                },
                                # Row without a token — dropped here (the
                                # identity view keeps it; this view is the
                                # subset of issued tokens only).
                                {"account_id": "bob@example.com", "enabled": True},
                            ]
                        },
                        "chatgpt": {
                            "accounts": [
                                {
                                    "account_id": "default",
                                    "credential_token": "tok-chatgpt",
                                    "enabled": True,
                                }
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

    def test_returns_none_when_urlopen_raises(self):
        from scripts.sidecar_pkg.credentials import fetch_credential_tokens

        with patch("scripts.sidecar_pkg.tls.build_context", return_value=None):
            with patch("urllib.request.urlopen", side_effect=TimeoutError("nope")):
                result = fetch_credential_tokens("https://api.example.com")
                assert result is None

    def test_skips_disabled_rows(self):
        """Disabled rows contribute no tokens (PR #283 round-3 review)."""
        from scripts.sidecar_pkg.credentials import fetch_credential_tokens

        payload = json.dumps(
            {
                "config": {
                    "providers": {
                        "anthropic": {
                            "accounts": [
                                {
                                    "account_id": "default",
                                    "credential_token": "tok-default",
                                    "enabled": True,
                                },
                                {
                                    "account_id": "alice@example.com",
                                    "credential_token": "tok-alice",
                                    "enabled": False,
                                },
                            ]
                        }
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

        # Disabled row is filtered out.
        assert ("anthropic", "alice@example.com") not in result
        assert ("anthropic", "default") in result


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

    def test_replace_preserves_view_when_argument_is_none(self):
        """``replace(accounts=None, tokens=X)`` keeps the existing
        identity view, and vice versa. Lets ``run_collection`` skip
        either side independently when its fetch returned None (PR #283
        round-3 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            accounts={"anthropic": ["default"]},
            tokens={("anthropic", "default"): "tok"},
        )
        # Replace only tokens → identity view intact.
        cache.replace(tokens={("anthropic", "default"): "tok2"})
        assert cache.provider_accounts() == {"anthropic": ["default"]}
        assert cache.tokens == {("anthropic", "default"): "tok2"}

    def test_refresh_from_config_keeps_prior_snapshot_on_outage(self):
        """When the server is unreachable, ``refresh_from_config`` must
        return ``None`` AND leave the cache untouched — so the next cycle
        retries instead of silently working off an empty map for the
        entire TTL (PR #283 round-3 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            accounts={"anthropic": ["default"]},
            tokens={("anthropic", "default"): "tok"},
        )

        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            return_value=None,
        ):
            result = cache.refresh_from_config("https://api.example.com", fetch_tokens=True)

        assert result is None
        # Cache state untouched.
        assert cache.provider_accounts() == {"anthropic": ["default"]}
        assert cache.tokens == {("anthropic", "default"): "tok"}

    def test_refresh_from_config_single_round_trip_when_fetch_tokens_true(self):
        """``refresh_from_config(fetch_tokens=True)`` must share ONE
        underlying HTTP fetch — both views deserialize from the same
        payload (PR #283 round-3 review). Two consecutive
        ``_fetch_config_payload`` calls (one per view) would double the
        request load on every heartbeat."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        payload = {
            "config": {
                "providers": {
                    "anthropic": {
                        "accounts": [
                            {
                                "account_id": "default",
                                "credential_token": "tok",
                                "enabled": True,
                            }
                        ]
                    }
                }
            }
        }

        call_count = {"n": 0}

        def _counted_fetch(*args, **kwargs):
            call_count["n"] += 1
            return payload

        cache = CredentialCache()
        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            side_effect=_counted_fetch,
        ):
            result = cache.refresh_from_config("https://api.example.com", fetch_tokens=True)

        assert result == (1, 1)
        assert call_count["n"] == 1, (
            "refresh_from_config(fetch_tokens=True) should hit the server once, "
            f"not {call_count['n']} times — share the payload between views."
        )
        assert cache.tokens == {("anthropic", "default"): "tok"}

    def test_refresh_from_config_returns_cache_token_count_when_not_fetching(self):
        """When ``fetch_tokens=False``, no token fetch happens, but the
        cache may still hold tokens from an earlier call. The returned
        tuple's token count should describe the cache, not this call —
        otherwise dashboards and health checks that read the return
        value see a misleading 0 (PR #283 round-4 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            accounts={"anthropic": ["default"]},
            tokens={("anthropic", "default"): "tok-old"},
        )

        payload = {
            "config": {
                "providers": {
                    "anthropic": {"accounts": [{"account_id": "default", "enabled": True}]}
                }
            }
        }

        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            return_value=payload,
        ):
            account_count, token_count = cache.refresh_from_config(
                "https://api.example.com", fetch_tokens=False
            )

        # account count is what we just fetched; token count is the
        # cache state — still holding the pre-existing token even though
        # we didn't refetch.
        assert account_count == 1
        assert token_count == 1, (
            "refresh_from_config(fetch_tokens=False) must report the cache's "
            "token count, not a fresh-fetch-only count — callers see the cache "
            "not the network call."
        )
        assert cache.tokens == {("anthropic", "default"): "tok-old"}


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
    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: lambda *x: [])
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: lambda *x: [])
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: lambda *x: [])
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: [])

    config = {"api_url": "https://api.example.com"}
    _, _, errors = sc.run_collection(config=config, providers=["anthropic"])

    assert errors == 0
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
