"""Unit tests for sidecar credentials fetchers (fetch_identity_hints & fetch_credential_tokens).

Covers:
- ``fetch_identity_hints`` single round-trip returning both identity + tag-hint views,
  outage tolerance, disabled rows filtering, and tag hints parsing.
- ``fetch_credential_tokens`` HTTP plumbing, token parsing, and disabled row filtering.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tests.unit._sidecar_credentials_helpers import (  # noqa: F401
    approved_keychain_readers,
    isolate_host_credentials,
)


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
            with patch("scripts.sidecar_pkg.transport.urlopen", side_effect=TimeoutError("nope")):
                result = fetch_identity_hints("https://api.example.com")
                assert result is None

    def test_returns_none_on_non_200(self):
        from scripts.sidecar_pkg.credentials import fetch_identity_hints

        with patch("scripts.sidecar_pkg.tls.build_context", return_value=None):
            with patch("scripts.sidecar_pkg.transport.urlopen") as mock_urlopen:
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
            with patch("scripts.sidecar_pkg.transport.urlopen") as mock_urlopen:
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
            with patch("scripts.sidecar_pkg.transport.urlopen") as mock_urlopen:
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
            with patch("scripts.sidecar_pkg.transport.urlopen") as mock_urlopen:
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
            with patch("scripts.sidecar_pkg.transport.urlopen", side_effect=_counted):
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
            with patch("scripts.sidecar_pkg.transport.urlopen") as mock_urlopen:
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
            with patch("scripts.sidecar_pkg.transport.urlopen", side_effect=TimeoutError("nope")):
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
            with patch("scripts.sidecar_pkg.transport.urlopen") as mock_urlopen:
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
