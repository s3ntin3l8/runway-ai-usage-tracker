"""Unit tests for credential candidate ownership and origin attribution.

Covers:
- Disambiguating multiple candidate credentials for a single provider (CLI, cookies, env).
- Ensuring separate owners for distinct credential origins.
- Reporting unverifiable provider origins without shipping tokens.
- Avoiding duplicate cookie candidates for env aliases.
"""

from __future__ import annotations

import json

from tests.unit._sidecar_credentials_helpers import (  # noqa: F401
    approved_keychain_readers,
    isolate_host_credentials,
)


class TestCredentialCandidateOwnership:
    @staticmethod
    def _jwt(email: str) -> str:
        import base64

        payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode())
        return "header." + payload.decode().rstrip("=") + ".signature"

    def test_chatgpt_cli_and_cookie_keep_separate_owners(self, tmp_path, monkeypatch):
        import scripts.sidecar as sc

        monkeypatch.setattr(sc.platform, "system", lambda: "Linux")

        auth_file = tmp_path / "auth.json"
        auth_file.write_text(
            json.dumps(
                {"tokens": {"access_token": "cli-a", "id_token": self._jwt("a@example.com")}}
            )
        )
        monkeypatch.setattr(
            sc.BrowserCookieExtractor,
            "get_cookie",
            staticmethod(lambda _domain, name, *_: f"cookie-{name}"),
        )
        config = {
            "name": "ChatGPT",
            "rules": [
                {
                    "type": "cookie",
                    "domains": ["chatgpt.com"],
                    "name": "__Secure-next-auth.session-token",
                    "mapping": {"value": "cookie_session"},
                },
                {
                    "type": "cookie",
                    "domains": ["chatgpt.com"],
                    "name": "oai-sc",
                    "mapping": {"value": "cookie_oai-sc"},
                },
                {
                    "type": "file",
                    "paths": [str(auth_file)],
                    "mapping": {
                        "tokens.access_token": "oauth_token",
                        "tokens.id_token": "id_token",
                    },
                },
            ],
        }
        cards, blocked = sc.GenericCollector.collect_provider("chatgpt", config)
        # The CLI file is identified by its id_token. The cookie has no identity of its own, so
        # it ships pending (the server verifies it, #460) and stays a separate owner.
        by_origin = {c["metadata"]["credential_origin"]: c for c in cards}
        cli = by_origin[f"path:{auth_file}"]
        assert cli["metadata"]["account_id"] == "a@example.com"
        assert cli["metadata"]["oauth_token"] == "cli-a"
        cookie = by_origin["cookie:chatgpt/session"]
        assert cookie["metadata"]["identity_pending"] is True
        assert "account_id" not in cookie["metadata"]
        assert blocked == [
            {"provider_id": "chatgpt", "credential_origin": "cookie:chatgpt/session"}
        ]

        tagged, blocked = sc.GenericCollector.collect_provider(
            "chatgpt",
            config,
            account_label_hints={"chatgpt": {"cookie:chatgpt/session": "b@example.com"}},
        )
        assert {c["metadata"]["account_id"] for c in tagged} == {
            "a@example.com",
            "b@example.com",
        }
        browser_card = next(c for c in tagged if c["unit"] == "cookie")
        assert browser_card["metadata"]["cookie_session"]
        assert browser_card["metadata"]["cookie_oai-sc"]
        assert blocked == []

    def test_claude_cli_env_and_browser_cookie_are_independent(self, monkeypatch):
        import scripts.sidecar as sc

        monkeypatch.setattr(sc.platform, "system", lambda: "Darwin")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cli-a")
        monkeypatch.setattr(
            sc.BrowserCookieExtractor,
            "get_macos_candidates",
            staticmethod(
                lambda *_args: [("browser-b", "cookie:anthropic/Chrome/Profile 4/Network/Cookies")]
            ),
        )
        config = {
            "name": "Claude",
            "rules": [
                {
                    "type": "env",
                    "variable": "CLAUDE_CODE_OAUTH_TOKEN",
                    "mapping": {"value": "oauth_token"},
                },
                {
                    "type": "cookie",
                    "domains": ["claude.ai"],
                    "name": "sessionKey",
                    "mapping": {"value": "cookie_sessionKey"},
                },
            ],
        }
        cards, blocked = sc.GenericCollector.collect_provider("anthropic", config)
        assert len(cards) == 2
        assert all(card["metadata"]["identity_pending"] for card in cards)
        assert {entry["credential_origin"] for entry in blocked} == {
            "env:CLAUDE_CODE_OAUTH_TOKEN",
            "cookie:anthropic/Chrome/Profile 4/Network/Cookies",
        }

        tagged, blocked = sc.GenericCollector.collect_provider(
            "anthropic",
            config,
            account_label_hints={
                "anthropic": {
                    "env:CLAUDE_CODE_OAUTH_TOKEN": "a@example.com",
                    "cookie:anthropic/Chrome/Profile 4/Network/Cookies": "b@example.com",
                }
            },
        )
        assert {c["metadata"]["account_id"] for c in tagged} == {
            "a@example.com",
            "b@example.com",
        }
        assert blocked == []

    def test_old_provider_tag_does_not_assign_browser_candidate(self, monkeypatch):
        import scripts.sidecar as sc

        monkeypatch.setattr(sc.platform, "system", lambda: "Darwin")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "unrelated-env-token")
        monkeypatch.setattr(
            sc.BrowserCookieExtractor,
            "get_macos_candidates",
            staticmethod(
                lambda *_args: [
                    ("browser-cookie", "cookie:anthropic/Chrome/Profile 4/Network/Cookies")
                ]
            ),
        )
        cards, blocked = sc.GenericCollector.collect_provider(
            "anthropic",
            {
                "rules": [
                    {
                        "type": "env",
                        "variable": "CLAUDE_CODE_OAUTH_TOKEN",
                        "mapping": {"value": "oauth_token"},
                    },
                    {
                        "type": "cookie",
                        "domains": ["claude.ai"],
                        "name": "sessionKey",
                        "mapping": {"value": "cookie_sessionKey"},
                    },
                ]
            },
            account_label_hints={"anthropic": {"provider:anthropic": "a@example.com"}},
        )
        assert len(cards) == 2
        assert all(card["metadata"]["identity_pending"] for card in cards)
        assert {entry["credential_origin"] for entry in blocked} == {
            "env:CLAUDE_CODE_OAUTH_TOKEN",
            "cookie:anthropic/Chrome/Profile 4/Network/Cookies",
        }

    def test_unverifiable_provider_reports_origin_without_shipping_token(self, monkeypatch):
        import scripts.sidecar as sc

        monkeypatch.setenv("OLLAMA_API_KEY", "ollama-secret")
        cards, blocked = sc.GenericCollector.collect_provider(
            "ollama",
            {
                "rules": [
                    {
                        "type": "env",
                        "variable": "OLLAMA_API_KEY",
                        "mapping": {"value": "api_key"},
                    }
                ]
            },
        )

        assert cards == []
        assert len(blocked) == 1
        assert blocked[0]["provider_id"] == "ollama"
        assert blocked[0]["credential_origin"].startswith("env:OLLAMA_API_KEY#")

    def test_env_cookie_alias_does_not_emit_duplicate_cookie_candidate(self, monkeypatch):
        import scripts.sidecar as sc

        monkeypatch.setattr(sc.platform, "system", lambda: "Linux")

        monkeypatch.setenv("OLLAMA_SESSION_TOKEN", "env-cookie")
        monkeypatch.setattr(
            sc.BrowserCookieExtractor,
            "get_cookie",
            staticmethod(lambda _domain, _name, *_: "browser-cookie"),
        )
        cards, blocked = sc.GenericCollector.collect_provider(
            "ollama",
            {
                "rules": [
                    {
                        "type": "env",
                        "variable": "OLLAMA_SESSION_TOKEN",
                        "mapping": {"value": "cookie_session"},
                    },
                    {
                        "type": "cookie",
                        "domains": ["ollama.com"],
                        "name": "session",
                        "mapping": {"value": "cookie_session"},
                    },
                ]
            },
            account_label_hints={"ollama": {"cookie:ollama/session": "a@example.com"}},
        )
        assert len(cards) == 1
        assert cards[0]["metadata"]["cookie_session"] == "browser-cookie"
        assert blocked == []
