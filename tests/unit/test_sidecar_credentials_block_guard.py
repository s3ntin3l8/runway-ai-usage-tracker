"""Unit tests for the sidecar silent-listener block guard (PR #288 / #290).

Covers:
- Dropping token cards whose ``account_id`` cannot be resolved via local discovery
  or server hints, and recording origin for manifest reporting.
- Origin hint fallback unblocking cards on the same cycle.
- Local discovery priority over hints.
- Warning logs on blocked token cards.
- Antigravity card reporting installed CLI version.
"""

from __future__ import annotations

from tests.unit._sidecar_credentials_helpers import (  # noqa: F401
    approved_keychain_readers,
    isolate_host_credentials,
)


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
        assert len(cards) == 1
        assert cards[0]["metadata"]["identity_pending"] is True
        assert cards[0]["account_id"] is None
        assert len(blocked) == 1, "exactly one blocked origin expected"
        assert blocked[0]["provider_id"] == "antigravity"

    def test_withheld_token_is_reported_with_a_reason_and_verifiable_ones_are_not(
        self, monkeypatch
    ):
        """The dashboard warns only about origins whose token stayed on the machine: a
        provider with no server-side verifier ships nothing, so quota will not collect."""
        import scripts.sidecar as sc

        monkeypatch.setenv("SOME_DEEPSEEK_KEY", "sk-test-123")
        rules = {
            "name": "x",
            "icon": "x",
            "rules": [
                {"type": "env", "variable": "SOME_DEEPSEEK_KEY", "mapping": {"value": "api_key"}}
            ],
        }
        monkeypatch.setattr(sc, "_ag_account_email", lambda: None)

        cards, blocked = sc.GenericCollector.collect_provider("deepseek", rules)
        assert cards == []  # the token stays on this machine
        assert [b.get("reason") for b in blocked] == ["token_withheld"]

        # A provider the server can verify by source ships its token, so nothing is withheld.
        cards, blocked = sc.GenericCollector.collect_provider("antigravity", rules)
        assert len(cards) == 1
        assert [b.get("reason") for b in blocked] == [None]

    def test_antigravity_card_reports_the_installed_agy_version(self, monkeypatch):
        """The sidecar ships the installed CLI's version next to the token (#687).

        The Code Assist API gates *which quota pools* it serves on the
        User-Agent's version, and the server never sees the CLI — only this
        host does.
        """
        import scripts.sidecar as sc
        from scripts.sidecar_pkg import agy_version

        monkeypatch.setenv("SOME_AGY_KEY", "sk-test-123")
        monkeypatch.setattr(sc, "_ag_account_email", lambda: "user@example.com")
        monkeypatch.setattr(agy_version, "installed_version", lambda: "1.4.3")
        rules = {
            "name": "Antigravity",
            "icon": "x",
            "rules": [
                {
                    "type": "env",
                    "variable": "SOME_AGY_KEY",
                    "mapping": {"value": "api_key"},
                }
            ],
        }

        cards, _blocked = sc.GenericCollector.collect_provider("antigravity", rules)

        assert len(cards) == 1
        assert cards[0]["metadata"]["agy_version"] == "1.4.3"

    def test_no_version_reported_when_the_cli_is_not_installed(self, monkeypatch):
        """An unknown version is simply absent — the server falls back on its own."""
        import scripts.sidecar as sc
        from scripts.sidecar_pkg import agy_version

        monkeypatch.setenv("SOME_AGY_KEY", "sk-test-123")
        monkeypatch.setattr(sc, "_ag_account_email", lambda: "user@example.com")
        monkeypatch.setattr(agy_version, "installed_version", lambda: None)
        rules = {
            "name": "Antigravity",
            "icon": "x",
            "rules": [
                {
                    "type": "env",
                    "variable": "SOME_AGY_KEY",
                    "mapping": {"value": "api_key"},
                }
            ],
        }

        cards, _blocked = sc.GenericCollector.collect_provider("antigravity", rules)

        assert len(cards) == 1
        assert "agy_version" not in cards[0]["metadata"]

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
        assert len(cards) == 1
        assert cards[0]["metadata"]["identity_pending"] is True
        assert cards[0]["account_id"] is None
        assert cards[0]["account_label"] is None
        assert "account_label" not in cards[0]["metadata"]
        assert blocked == [
            {"provider_id": "antigravity", "credential_origin": "env:ANTHROPIC_API_KEY"}
        ]

    def test_origin_hint_resolves_unknown_environment_without_cli_fallback(self, monkeypatch):
        """When local discovery yields an ``account_id`` AND a server
        hint exists, the locally-discovered value wins. The hint is the
        fallback, not the override — local discovery has tighter scope
        (the host's own file/env/keychain) and is the more authoritative
        identity."""
        import scripts.sidecar as sc

        monkeypatch.setenv("RUNWAY_TEST_VALUE", "sk-test-123")
        # An environment token has no identity of its own, so the matching
        # environment-origin tag resolves it.
        monkeypatch.setattr(sc, "_ag_account_email", lambda: None)
        monkeypatch.setattr(sc, "_codex_account_email", lambda: "unrelated@example.com")
        cards, blocked = sc.GenericCollector.collect_provider(
            "chatgpt",
            {
                "name": "ChatGPT",
                "icon": "💬",
                "rules": [
                    {
                        "type": "env",
                        "variable": "RUNWAY_TEST_VALUE",
                        "mapping": {"value": "api_key"},
                    },
                ],
            },
            account_label_hints={
                "chatgpt": {"env:RUNWAY_TEST_VALUE": "tagged-env@example.com"},
            },
        )
        assert len(cards) == 1
        assert cards[0]["metadata"]["account_id"] == "tagged-env@example.com", (
            "the credential's own origin hint should resolve it"
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
        assert len(cards) == 1
        assert all(card["metadata"]["identity_pending"] for card in cards)
        assert len(blocked) == 1
        assert blocked[0]["provider_id"] == "antigravity"
