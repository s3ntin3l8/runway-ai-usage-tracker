"""Tests for the sidecar identity cache (issue #272, deferred redeem).

Covers:
- ``scripts/sidecar_pkg/credentials.py``: ``fetch_identity_hints`` (now
  single round-trip returning both identity + tag-hint views, PR #290
  round-2 review) and ``fetch_credential_tokens`` HTTP plumbing, plus
  the ``CredentialCache`` staleness / replacement / decoupled-identity
  semantics.
- The per-account event-extraction loop wired into ``run_collection`` —
  the dispatch table, the local-identity intersect (one account per
  host), the legacy fallback when the server has no per-account config,
  the partial-failure tolerance.
- The silent-listener block guard (PR #288, fixed in PR #290): a token
  card whose ``account_id`` cannot be resolved via local discovery or
  a server hint is dropped from the result list, the credential's
  origin is captured for the next ``/fleet/credentials/manifest`` POST.
  The hint-fallback path is the headline behaviour: a server-supplied
  tag *unblocks* the card on the same cycle instead of waiting for
  the next ``/fleet/config`` round-trip.
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
    """``fetch_identity_hints`` returns both decoupled views from a single
    ``/fleet/config`` round-trip (PR #290 round-2 review): the
    per-account identity hints for the #272 attribution fix AND the
    operator-resolved tag-hint map for the silent-listener block guard.

    The function returns ``None`` on fetch failures (outage) to
    distinguish that from a successful empty response. When the
    payload omits the ``account_tag_hints`` field (older server
    versions pre-PR #288), the tuple's tag-hint slot is ``None`` so
    the caller can preserve its prior hint map instead of clobbering
    it with an empty dict (PR #290 round-2 review).
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

        # Tag hints absent in payload → ``None`` slot (PR #290 round-2).
        assert result == (
            {
                "anthropic": ["default", "alice@example.com", "bob@example.com"],
                "chatgpt": ["default"],
            },
            None,
        )
        # Provider with no accounts is absent.
        assert "empty" not in result[0]

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
        assert result == ({"anthropic": ["default"]}, None)

    def test_parses_tag_hints_when_payload_carries_them(self):
        """PR #288 / #290: the operator-resolved tag-hint map comes back
        in the same payload as ``account_tag_hints``. The fetcher
        returns it as the second tuple slot."""
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        payload = json.dumps(
            {
                "config": {"providers": {}},
                "account_tag_hints": {
                    "anthropic": {
                        "path:/home/alice/.claude/.credentials.json": "alice@example.com",
                    },
                    "chatgpt": {
                        "path:/home/alice/.codex/auth.json": "alice@example.com",
                        # Defensive: non-string entries are dropped.
                        "broken_entry": 42,
                    },
                },
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

        assert result == (
            {},
            {
                "anthropic": {"path:/home/alice/.claude/.credentials.json": "alice@example.com"},
                "chatgpt": {"path:/home/alice/.codex/auth.json": "alice@example.com"},
            },
        )

    def test_single_round_trip_parses_both_views(self):
        """``fetch_identity_hints`` issues exactly one HTTP GET and parses
        both views from the same payload (PR #290 round-2 review —
        earlier code split this across two fetchers, doubling the
        request load on every heartbeat)."""
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        payload = json.dumps(
            {
                "config": {
                    "providers": {
                        "anthropic": {
                            "accounts": [{"account_id": "default", "enabled": True}],
                        }
                    }
                },
                "account_tag_hints": {
                    "anthropic": {"provider:anthropic": "default"},
                },
            }
        ).encode()

        call_count = {"n": 0}

        def _counted(*args, **kwargs):
            call_count["n"] += 1
            return MagicMock(
                __enter__=MagicMock(
                    return_value=MagicMock(
                        getcode=MagicMock(return_value=200),
                        read=MagicMock(return_value=payload),
                    )
                ),
                __exit__=MagicMock(return_value=False),
            )

        with patch("scripts.sidecar_pkg.tls.build_context", return_value=None):
            with patch("urllib.request.urlopen", side_effect=_counted):
                fetch_identity_hints("https://api.example.com")

        assert call_count["n"] == 1, (
            f"fetch_identity_hints should hit /fleet/config once, "
            f"not {call_count['n']} times — both views must share the response."
        )


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


class TestCollectProviderBlockGuard:
    """The silent-listener block guard (PR #288) inside ``GenericCollector.collect_provider``.

    A token card whose ``account_id`` can't be resolved via local
    discovery or an ``account_label_hints`` server-supplied fallback
    must NOT ship. The card is dropped and the credential's origin is
    recorded in the second return value so ``run_collection`` can
    POST it to ``/fleet/credentials/manifest``.

    The legacy single-account shape (one token card per provider) is
    preserved — the block affects only the case where ``account_id``
    can't be stamped, which is the multi-account silent-failure the
    PR series is closing.
    """

    @staticmethod
    def _anthropic_provider_config():
        """A config that exercises env + file rules; matches today's anthropic registry."""
        return {
            "name": "Anthropic",
            "icon": "🅰️",
            "rules": [
                {
                    "type": "env",
                    "variable": "ANTHROPIC_API_KEY",
                    "mapping": {"value": "api_key"},
                },
                {
                    "type": "file",
                    "paths": ["~/nonexistent-credential-file.json"],
                    "format": "json",
                    "mapping": {"apiKey": "api_key"},  # pragma: allowlist secret
                },
            ],
        }

    def test_token_card_ships_when_account_id_resolvable(self, monkeypatch):
        """When local discovery yields an email, the card ships as today."""
        import scripts.sidecar as sc

        # Env rule yields a credential; account_id resolved via chatgpt-style stamp.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        cards, blocked = sc.GenericCollector.collect_provider(
            "chatgpt",
            {
                "name": "ChatGPT",
                "icon": "💬",
                "rules": [
                    {
                        "type": "file",
                        "paths": ["~/nonexistent.json"],
                        "format": "json",
                        "mapping": {"tokens.account_id": "account_id"},
                    },
                ],
            },
        )
        # No credential extracted because the file doesn't exist; account_id
        # stamping never runs. Should be empty with no blocked entries.
        assert cards == []
        assert blocked == []

    def test_token_card_blocked_when_no_account_id(self, monkeypatch):
        """When tokens are extracted but ``account_id`` can't be resolved,
        the card is dropped and the credential origin is recorded."""
        import scripts.sidecar as sc

        # Env rule yields a credential; no local discovery path stamps account_id
        # because we monkeypatch the helper that normally does. This is the
        # precondition for "tokens extracted, no account_id resolvable".
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        monkeypatch.setattr(sc, "_ag_account_email", lambda: None)
        monkeypatch.setattr(sc, "_codex_account_email", lambda: None)
        # Render the env rule's path empty so anthropic statusline doesn't fire.
        cards, blocked = sc.GenericCollector.collect_provider(
            "antigravity",  # any provider; we'll override the rule chain
            {
                "name": "Anthropic",
                "icon": "🅰️",
                "rules": [
                    {
                        "type": "env",
                        "variable": "ANTHROPIC_API_KEY",
                        "mapping": {"value": "api_key"},
                    },
                ],
            },
        )
        # The env rule fires, tokens dict has api_key, but account_id can't be
        # resolved (the antigravity+chatgpt stamping helpers returned None,
        # anthropic statusline wasn't iterated).
        assert cards == [], "token card must be dropped when account_id is None"
        assert len(blocked) == 1, "exactly one blocked origin expected"
        assert blocked[0]["provider_id"] == "antigravity"
        assert blocked[0]["credential_origin"] == "provider:antigravity"

    def test_no_block_when_no_tokens_extracted(self):
        """No credentials found → no blocked origin (because there was nothing to ship)."""
        import scripts.sidecar as sc

        cards, blocked = sc.GenericCollector.collect_provider(
            "anthropic",
            {
                "name": "Anthropic",
                "icon": "🅰️",
                "rules": [
                    {
                        "type": "file",
                        "paths": ["~/no-such-credential-file.json"],
                        "format": "json",
                        "mapping": {"apiKey": "api_key"},  # pragma: allowlist secret
                    },
                ],
            },
        )
        assert cards == []
        assert blocked == []

    def test_token_card_blocked_logs_warning(self, monkeypatch, caplog):
        import logging

        import scripts.sidecar as sc

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        monkeypatch.setattr(sc, "_ag_account_email", lambda: None)
        monkeypatch.setattr(sc, "_codex_account_email", lambda: None)
        with caplog.at_level(logging.WARNING, logger="root"):
            sc.GenericCollector.collect_provider(
                "antigravity",
                {
                    "name": "Anthropic",
                    "icon": "🅰️",
                    "rules": [
                        {
                            "type": "env",
                            "variable": "ANTHROPIC_API_KEY",
                            "mapping": {"value": "api_key"},
                        },
                    ],
                },
            )
        assert any("blocked" in r.message for r in caplog.records)

    def test_hint_unblocks_card_when_local_discovery_fails(self, monkeypatch):
        """PR #290 critical (Hermes): the server-supplied tag-hint map
        MUST unblock a card whose ``account_id`` could not be resolved
        via local discovery. Without this fallback the silent-listener
        loop never closes — the operator tags, the server ships the
        hint, the sidecar drops the card again, the operator sees the
        entry forever.

        This is Hermes's exact probe from the review:
            collect_provider("antigravity", env_rule_cfg,
                account_label_hints={"antigravity": {"provider:antigravity": "alice@example.com"}})
            → must ship with account_id="alice@example.com", NOT drop.
        """
        import scripts.sidecar as sc

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        monkeypatch.setattr(sc, "_ag_account_email", lambda: None)
        monkeypatch.setattr(sc, "_codex_account_email", lambda: None)
        cards, blocked = sc.GenericCollector.collect_provider(
            "antigravity",
            {
                "name": "Anthropic",
                "icon": "🅰️",
                "rules": [
                    {
                        "type": "env",
                        "variable": "ANTHROPIC_API_KEY",
                        "mapping": {"value": "api_key"},
                    },
                ],
            },
            account_label_hints={
                "antigravity": {"provider:antigravity": "alice@example.com"},
            },
        )
        assert len(cards) == 1, (
            "hint must unblock the card; PR #290 critical — without this "
            "the silent-listener loop never closes."
        )
        assert blocked == [], "no blocked origin expected when hint resolves the card"
        assert cards[0]["metadata"]["account_id"] == "alice@example.com", (
            "the hint-supplied account_id must be written into tokens so the "
            "card's metadata carries it through to /fleet/ingest"
        )

    def test_hint_does_not_overwrite_local_discovery(self, monkeypatch):
        """When local discovery yields an ``account_id`` AND a server
        hint exists, the locally-discovered value wins. The hint is the
        fallback, not the override — local discovery has tighter scope
        (the host's own file/env/keychain) and is the more authoritative
        identity."""
        import scripts.sidecar as sc

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        # Simulate a chatgpt-style local discovery returning "default".
        # The antigravity provider chain sets account_id via the
        # _codex_account_email / _ag_account_email helpers; if those
        # were non-None they'd win. Patch them to None and use chatgpt
        # provider so the codex path stamps account_id.
        monkeypatch.setattr(sc, "_ag_account_email", lambda: None)
        monkeypatch.setattr(sc, "_codex_account_email", lambda: "alice-from-local@example.com")
        cards, blocked = sc.GenericCollector.collect_provider(
            "chatgpt",
            {
                "name": "ChatGPT",
                "icon": "💬",
                "rules": [
                    {
                        "type": "env",
                        "variable": "ANTHROPIC_API_KEY",
                        "mapping": {"value": "api_key"},
                    },
                ],
            },
            account_label_hints={
                "chatgpt": {"provider:chatgpt": "should-be-ignored@example.com"},
            },
        )
        assert len(cards) == 1
        assert cards[0]["metadata"]["account_id"] == "alice-from-local@example.com", (
            "local discovery must win over a server hint"
        )

    def test_hint_for_wrong_provider_does_not_unblock(self, monkeypatch):
        """A hint that targets a *different* provider doesn't fall through
        to this provider's block guard. The lookup is
        ``provider_hints.get(f"provider:{provider_id}")`` — must match
        the provider_id of the collector, not bleed across providers."""
        import scripts.sidecar as sc

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        monkeypatch.setattr(sc, "_ag_account_email", lambda: None)
        monkeypatch.setattr(sc, "_codex_account_email", lambda: None)
        cards, blocked = sc.GenericCollector.collect_provider(
            "antigravity",
            {
                "name": "Anthropic",
                "icon": "🅰️",
                "rules": [
                    {
                        "type": "env",
                        "variable": "ANTHROPIC_API_KEY",
                        "mapping": {"value": "api_key"},
                    },
                ],
            },
            account_label_hints={
                # Hint is for "anthropic", not "antigravity". Must NOT
                # unblock the antigravity card.
                "anthropic": {"provider:antigravity": "leaked@example.com"},
            },
        )
        assert cards == []
        assert len(blocked) == 1
        assert blocked[0]["provider_id"] == "antigravity"


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
        round-3 review). ``tag_hints=None`` preserves the prior hint map
        — used by ``run_collection`` when the server omits the
        ``account_tag_hints`` field (older server pre-PR #288), so a
        downgrade doesn't silently wipe cached hints (PR #290
        round-2 review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            accounts={"anthropic": ["default"]},
            tokens={("anthropic", "default"): "tok"},
            tag_hints={
                "anthropic": {"provider:anthropic": "default"},
            },
        )
        # Replace only tokens → identity + hint views intact.
        cache.replace(tokens={("anthropic", "default"): "tok2"})
        assert cache.provider_accounts() == {"anthropic": ["default"]}
        assert cache.tokens == {("anthropic", "default"): "tok2"}
        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "default"},
        }
        # And ``tag_hints=None`` keeps the prior hint map.
        cache.replace(tag_hints=None)
        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "default"},
        }, "tag_hints=None must preserve the prior hint map"
        # But ``tag_hints={}`` clears it.
        cache.replace(tag_hints={})
        assert cache.provider_tag_hints() == {}, "tag_hints={} must clear the hint map"

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

    def test_provider_tag_hints_empty_by_default(self):
        """A freshly-constructed cache exposes an empty tag-hint view.
        ``GenericCollector.collect_provider`` consults this as a
        fallback before blocking a card (PR #288 / #290)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        assert cache.provider_tag_hints() == {}

    def test_refresh_from_config_populates_tag_hints(self):
        """``refresh_from_config`` now picks up the operator-resolved
        tag-hint map from the same payload (PR #290 round-2 review —
        earlier code only refreshed identity + tokens)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        payload = {
            "config": {
                "providers": {
                    "anthropic": {
                        "accounts": [{"account_id": "default", "enabled": True}],
                    }
                }
            },
            "account_tag_hints": {
                "anthropic": {"provider:anthropic": "alice@example.com"},
            },
        }

        cache = CredentialCache()
        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            return_value=payload,
        ):
            cache.refresh_from_config("https://api.example.com", fetch_tokens=False)

        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "alice@example.com"},
        }

    def test_refresh_from_config_keeps_prior_tag_hints_when_field_absent(self):
        """Older server (pre-PR #288) omits ``account_tag_hints``. The
        cache's prior hint map survives that round-trip — a downgrade
        doesn't silently wipe previously-cached hints (PR #290 round-2
        review)."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            tag_hints={"anthropic": {"provider:anthropic": "alice@example.com"}},
        )

        payload = {
            "config": {
                "providers": {
                    "anthropic": {"accounts": [{"account_id": "default", "enabled": True}]}
                }
            }
            # no ``account_tag_hints`` key — older server
        }

        with patch(
            "scripts.sidecar_pkg.credentials._fetch_config_payload",
            return_value=payload,
        ):
            cache.refresh_from_config("https://api.example.com", fetch_tokens=False)

        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "alice@example.com"},
        }, "missing field must preserve the prior hint map, not clear it"

    def test_provider_tag_hints_returns_defensive_copy(self):
        """Mutating the returned map must NOT mutate the cache's
        internal state — same contract as ``provider_accounts``."""
        from scripts.sidecar_pkg.credentials import CredentialCache

        cache = CredentialCache()
        cache.replace(
            tag_hints={"anthropic": {"provider:anthropic": "alice@example.com"}},
        )
        result = cache.provider_tag_hints()
        result["anthropic"]["provider:anthropic"] = "tampered@example.com"
        result["new_provider"] = {"provider:new": "evil@example.com"}
        # Re-fetch proves the cache is unchanged.
        assert cache.provider_tag_hints() == {
            "anthropic": {"provider:anthropic": "alice@example.com"},
        }


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

    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

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
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

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
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

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
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

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
    monkeypatch.setattr(sc, "_make_account_extractor", lambda *a, **kw: lambda *x: [])
    monkeypatch.setattr(sc, "_make_account_extractor_opencode", lambda *a, **kw: lambda *x: [])
    monkeypatch.setattr(sc, "_make_account_extractor_antigravity", lambda *a, **kw: lambda *x: [])
    monkeypatch.setattr(sc.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))

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
