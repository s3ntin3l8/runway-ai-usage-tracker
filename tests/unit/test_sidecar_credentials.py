"""Tests for the sidecar credential pipeline (issue #272).

Covers:
- ``scripts/sidecar_pkg/credentials.py``: ``fetch_credential_tokens`` and
  ``redeem_credential`` HTTP plumbing, plus the ``CredentialCache``
  staleness / eviction semantics.
- The per-account event-extraction loop wired into ``run_collection`` —
  the dispatch table, the legacy fallback when the server has no per-account
  config, the partial-failure tolerance.
"""

from __future__ import annotations

import json
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


class TestFetchCredentialTokens:
    def test_returns_empty_when_urlopen_raises(self):
        from scripts.sidecar_pkg.credentials import fetch_credential_tokens

        with patch("scripts.sidecar_pkg.credentials.build_context", return_value=None):
            with patch("urllib.request.urlopen", side_effect=TimeoutError("nope")):
                assert fetch_credential_tokens("https://api.example.com") == {}

    def test_returns_empty_on_non_200(self):
        from scripts.sidecar_pkg.credentials import fetch_credential_tokens

        with patch("scripts.sidecar_pkg.credentials.build_context", return_value=None):
            with patch("urllib.request.urlopen") as mock_urlopen:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(
                    return_value=MagicMock(getcode=MagicMock(return_value=500))
                )
                ctx.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = ctx
                assert fetch_credential_tokens("https://api.example.com") == {}

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
        with patch("scripts.sidecar_pkg.credentials.build_context", return_value=None):
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

        assert result == {
            ("anthropic", "default"): "tok-default",
            ("anthropic", "alice@example.com"): "tok-alice",
            ("chatgpt", "default"): "tok-chatgpt",
        }
        # Provider with no accounts is absent.
        assert ("empty", "default") not in result

    def test_skips_rows_without_token(self):
        from scripts.sidecar_pkg.credentials import fetch_credential_tokens

        payload = json.dumps(
            {
                "config": {
                    "providers": {
                        "openrouter": {
                            "accounts": [
                                {"account_id": "default"},
                                {
                                    "account_id": "alice@example.com",
                                    "credential_token": "tok-alice",
                                },
                            ]
                        }
                    }
                }
            }
        ).encode()
        with patch("scripts.sidecar_pkg.credentials.build_context", return_value=None):
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
        assert result == {("openrouter", "alice@example.com"): "tok-alice"}


class TestRedeemCredential:
    def test_returns_none_when_urlopen_fails(self):
        from scripts.sidecar_pkg.credentials import redeem_credential

        with patch("scripts.sidecar_pkg.credentials.build_context", return_value=None):
            with patch("urllib.request.urlopen", side_effect=TimeoutError):
                assert redeem_credential("https://api.example.com", "secret", "tok") is None

    def test_returns_none_on_non_200(self):
        from scripts.sidecar_pkg.credentials import redeem_credential

        with patch("scripts.sidecar_pkg.credentials.build_context", return_value=None):
            with patch("urllib.request.urlopen") as mock_urlopen:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(
                    return_value=MagicMock(getcode=MagicMock(return_value=401))
                )
                ctx.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = ctx
                assert redeem_credential("https://api.example.com", "secret", "tok") is None

    def test_returns_credentials_on_200(self):
        from scripts.sidecar_pkg.credentials import redeem_credential

        payload = json.dumps(
            {
                "provider_id": "openrouter",
                "account_id": "alice@example.com",
                "credentials": {"api_key": "sk-test"},
            }
        ).encode()
        with patch("scripts.sidecar_pkg.credentials.build_context", return_value=None):
            with patch("urllib.request.urlopen") as mock_urlopen:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(
                    return_value=MagicMock(
                        getcode=MagicMock(return_value=200), read=MagicMock(return_value=payload)
                    )
                )
                ctx.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = ctx
                assert redeem_credential("https://api.example.com", "secret", "tok") == {
                    "api_key": "sk-test"
                }


class TestCredentialCache:
    def test_provider_accounts_groups_by_provider(self):
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace_tokens(
            {
                ("anthropic", "default"): "t1",
                ("anthropic", "alice@example.com"): "t2",
                ("chatgpt", "default"): "t3",
            }
        )
        result = cache.provider_accounts()
        assert result == {
            "anthropic": ["default", "alice@example.com"],
            "chatgpt": ["default"],
        }

    def test_replace_drops_credentials_for_removed_pairs(self):
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace_tokens({("anthropic", "default"): "t"})
        cache._credentials[("anthropic", "default")] = {"api_key": "k"}
        # Re-fetch with a different account — old account's credentials dropped.
        cache.replace_tokens({("anthropic", "alice@example.com"): "t2"})
        assert ("anthropic", "default") not in cache.credentials
        assert ("anthropic", "alice@example.com") not in cache.credentials

    def test_redeem_caches_result(self):
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace_tokens({("anthropic", "default"): "tok"})
        with patch(
            "scripts.sidecar_pkg.credentials.redeem_credential",
            return_value={"api_key": "k"},
        ) as mock_redeem:
            creds1 = cache.redeem("http://x", "k", ("anthropic", "default"))
            creds2 = cache.redeem("http://x", "k", ("anthropic", "default"))
        assert creds1 == {"api_key": "k"}
        assert creds2 == {"api_key": "k"}
        # Second call should hit the cache, not the network.
        assert mock_redeem.call_count == 1

    def test_is_fresh(self):
        import time as _t

        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache(ttl_seconds=60)
        # No fetch yet → not fresh
        assert not cache.is_fresh()
        cache.replace_tokens({})
        baseline = _t.time()
        # Fresh immediately after fetch (delta 0 < 60)
        assert cache.is_fresh(now=baseline)
        # Stale after TTL
        assert not cache.is_fresh(now=baseline + 61)

    def test_redeem_returns_none_when_no_token_for_pair(self):
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace_tokens({})
        assert cache.redeem("http://x", "k", ("anthropic", "default")) is None

    def test_forget_clears_pair(self):
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace_tokens({("anthropic", "default"): "tok"})
        cache._credentials[("anthropic", "default")] = {"api_key": "k"}
        cache.forget(("anthropic", "default"))
        assert ("anthropic", "default") not in cache.tokens
        assert ("anthropic", "default") not in cache.credentials


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


def test_run_collection_iterates_per_account_events(monkeypatch, tmp_path):
    """End-to-end: ``run_collection`` fetches tokens, then iterates the
    per-account event extractors instead of the single-account legacy path.

    Mocks the token fetch to return two accounts for ``anthropic``; the
    extractor is called twice (once per account) and events from both
    accounts are appended.
    """
    import scripts.sidecar as sc

    # Stub the credential cache: pre-populate with two accounts for anthropic.
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace_tokens(
        {
            ("anthropic", "default"): "tok-default",
            ("anthropic", "alice@example.com"): "tok-alice",
        }
    )
    monkeypatch.setattr(sc, "_CREDENTIAL_CACHE", cache)

    # Stub the per-account dispatcher to record calls and emit per-account events.
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

    # Stub the metrics path so we don't actually try to scrape.
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: [])

    # Provide a minimal config; the sidecar reads `providers` from it for
    # legacy enable-list fallback, but since we set `providers=['anthropic']`
    # via the second arg we don't even need the config to know which
    # providers to enable.
    config: dict = {"api_url": "http://unused", "api_key": "secret"}

    metrics, events, errors = sc.run_collection(config=config, providers=["anthropic"])

    assert errors == 0
    # No metrics (stubbed to empty).
    assert metrics == []
    # Two accounts → two events.
    assert len(events) == 2
    assert {e["event_id"] for e in events} == {"evt-default", "evt-alice@example.com"}
    # The extractor was called once per account (in cache order).
    assert [e["account_id"] for e in emitted] == ["default", "alice@example.com"]


def test_run_collection_falls_back_to_legacy_when_no_server_accounts(monkeypatch):
    """When the server's /fleet/config has no per-account tokens for the
    provider, ``run_collection`` falls back to the legacy single-account
    path (local discovery + ``account_id="default"``)."""
    import scripts.sidecar as sc

    # Empty cache → no per-account tokens.
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace_tokens({})
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
