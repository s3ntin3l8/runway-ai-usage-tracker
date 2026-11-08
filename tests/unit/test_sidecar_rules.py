"""Unit tests for sidecar provider detection rules, token extraction, and expiry."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit._sidecar_helpers import (
    _REPO_ROOT,
    isolate_host_credentials,  # noqa: F401 — fixture
)

if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import sidecar


def _gemini_file_mapping(rules: list) -> dict:
    """The json-file rule mapping from a provider's detection rules."""
    for rule in rules:
        if rule.get("type") == "file" and rule.get("format") == "json":
            return rule.get("mapping", {})
    return {}


class TestOpenCodeCookieRules:
    """Console-cookie discovery ships with the collector that consumes it."""

    def test_console_cookie_is_present_in_both_registries(self):
        sidecar_rules = sidecar.__REGISTRY__["providers"]["opencode"]["rules"]
        registry = json.loads(
            (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
        )
        config_rules = registry["providers"]["opencode"]["rules"]

        for rules in (sidecar_rules, config_rules):
            cookie_names = {rule["name"] for rule in rules if rule.get("type") == "cookie"}
            assert "auth" in cookie_names
            assert "__Host-console_session" in cookie_names


class TestGeminiCredentialMapping:
    """Regression: the Gemini account email lives inside the OAuth id_token JWT.

    If the sidecar credential mapping omits id_token, the server can't derive the
    email and resolves account_id="default" — which split the dashboard into two
    Gemini cards (one quota-only "default", one stale email-keyed). Both the baked
    sidecar registry and the canonical registry.json must ship the id_token.
    """

    def test_baked_sidecar_registry_ships_id_token(self):
        gemini = sidecar.__REGISTRY__["providers"]["gemini"]
        mapping = _gemini_file_mapping(gemini["rules"])
        assert mapping.get("id_token") == "id_token"
        assert mapping.get("refresh_token") == "refresh_token"
        # expiry_date is the freshness signal that stops a stale local token from
        # clobbering a server-refreshed one (opaque ya29.* tokens carry no exp).
        assert mapping.get("expiry_date") == "expiry_date"

    def test_canonical_registry_ships_id_token(self):
        registry = json.loads(
            (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
        )
        gemini = registry["providers"]["gemini"]
        mapping = _gemini_file_mapping(gemini["rules"])
        assert mapping.get("id_token") == "id_token"
        assert mapping.get("refresh_token") == "refresh_token"
        assert mapping.get("expiry_date") == "expiry_date"
        # The email is not a top-level field in oauth_creds.json — it must come
        # from the id_token, never a phantom "email" -> account_id mapping.
        assert "email" not in mapping


class TestClaudeOAuthCredentialRules:
    def test_standard_claude_code_files_supply_token_email_and_expiry(self, monkeypatch, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        credentials_path = claude_dir / ".credentials.json"
        credentials_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "oauth-access-token",
                        "refreshToken": "oauth-refresh-token",
                        "expiresAt": 1790784000000,
                    }
                }
            )
        )
        (tmp_path / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "Alice@Example.com"},
                }
            )
        )
        monkeypatch.setattr(sidecar.Path, "home", staticmethod(lambda: tmp_path))
        original_expanduser = sidecar.os.path.expanduser
        monkeypatch.setattr(
            sidecar.os.path,
            "expanduser",
            lambda path: (
                str(tmp_path / path[2:])
                if isinstance(path, str) and path.startswith("~/")
                else original_expanduser(path)
            ),
        )
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [credentials_path])
        config = {
            "name": "Claude",
            "rules": [
                {
                    "type": "file",
                    "paths": [str(credentials_path)],
                    "format": "json",
                    "mapping": {
                        "claudeAiOauth.accessToken": "oauth_token",
                        "claudeAiOauth.refreshToken": "refresh_token",
                    },
                }
            ],
        }

        cards, entries = sidecar.GenericCollector.collect_provider("anthropic", config)

        assert cards[0]["account_id"] == "alice@example.com"
        assert cards[0]["metadata"]["expiry_date"] == "1790784000000"
        assert entries == [
            {
                "provider_id": "anthropic",
                "credential_origin": f"path:{credentials_path.resolve()}",
                "account_id": "alice@example.com",
            }
        ]

    def test_oauth_creds_path_and_identity_mapping_are_available_in_both_registries(self):
        registry = json.loads(
            (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
        )
        for providers in (sidecar.__REGISTRY__["providers"], registry["providers"]):
            rules = providers["anthropic"]["rules"]
            oauth_file = next(
                rule
                for rule in rules
                if rule.get("type") == "file"
                and "{{CONFIG_DIR:claude}}/oauth_creds.json" in rule.get("paths", [])
            )
            assert oauth_file["mapping"]["claudeAiOauth.accessToken"] == "oauth_token"
            assert oauth_file["mapping"]["claudeAiOauth.refreshToken"] == "refresh_token"
            assert oauth_file["mapping"]["oauthAccount.emailAddress|oauthAccount.email"] == (
                "account_id"
            )

    def test_sidecar_extracts_oauth_creds_token_and_account(self, monkeypatch, tmp_path):
        creds_path = tmp_path / "oauth_creds.json"
        creds_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "oauth-access-token",
                        "refreshToken": "oauth-refresh-token",
                        "clientId": "claude-client",
                    },
                    "oauthAccount": {"emailAddress": "Claude@Example.com"},
                }
            )
        )
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [creds_path])

        config = {
            **sidecar.__REGISTRY__["providers"]["anthropic"],
            "rules": [
                rule
                for rule in sidecar.__REGISTRY__["providers"]["anthropic"]["rules"]
                if rule.get("type") == "file"
                and "{{CONFIG_DIR:claude}}/oauth_creds.json" in rule.get("paths", [])
            ],
        }
        cards, blocked = sidecar.GenericCollector.collect_provider("anthropic", config)

        assert blocked == [
            {
                "provider_id": "anthropic",
                "credential_origin": f"path:{creds_path.resolve()}",
                "account_id": "claude@example.com",
            }
        ]
        token_card = next(card for card in cards if card["unit"] == "oauth")
        assert token_card["account_id"] == "claude@example.com"
        assert token_card["metadata"]["oauth_token"] == "oauth-access-token"
        assert token_card["metadata"]["refresh_token"] == "oauth-refresh-token"


class TestOpenCodeCredentialRules:
    def test_sidecar_registry_discovers_file_env_and_both_console_cookies(self):
        opencode = sidecar.__REGISTRY__["providers"]["opencode"]
        rules = opencode["rules"]
        file_rule = next(rule for rule in rules if rule.get("type") == "file")
        assert "~/.local/share/opencode/auth.json" in file_rule["paths"]
        assert "~/.opencode/auth.json" in file_rule["paths"]
        assert file_rule["mapping"]["opencode-go.key"] == "api_key"
        assert any(
            rule.get("type") == "env"
            and rule.get("variable") == "OPENCODE_API_KEY"
            and rule.get("mapping", {}).get("value") == "api_key"
            for rule in rules
        )
        cookie_slots = {
            rule["name"]: rule["mapping"]["value"] for rule in rules if rule.get("type") == "cookie"
        }
        assert cookie_slots["auth"] == "cookie_session"
        assert cookie_slots["__Host-console_session"] == "console_session"

    def test_canonical_registry_mirrors_sidecar_credential_rules(self):
        registry = json.loads(
            (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
        )
        rules = registry["providers"]["opencode"]["rules"]
        file_rule = next(rule for rule in rules if rule.get("type") == "file")
        assert file_rule["mapping"]["opencode-go.key"] == "api_key"
        assert any(rule.get("variable") == "OPENCODE_API_KEY" for rule in rules)
        cookie_slots = {rule["name"] for rule in rules if rule.get("type") == "cookie"}
        assert cookie_slots == {"auth", "__Host-console_session"}

    def test_open_code_registries_do_not_emit_sqlite_rolling_usage_cards(self):
        registry = json.loads(
            (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
        )
        for providers in (sidecar.__REGISTRY__["providers"], registry["providers"]):
            assert all(rule.get("type") != "sqlite" for rule in providers["opencode"]["rules"])


class TestClaudeStatuslineRules:
    def test_keeps_quota_windows_without_session_token_card(self, monkeypatch, tmp_path):
        statusline = tmp_path / "statusline.json"
        statusline.write_text(
            json.dumps(
                {
                    "rate_limits": {
                        "five_hour": {"used_percentage": 25, "resets_at": 1_800_000_000},
                        "seven_day": {"used_percentage": 40, "resets_at": 1_800_000_000},
                    },
                    "context_window": {
                        "total_input_tokens": 900,
                        "total_output_tokens": 100,
                        "max_tokens": 200_000,
                    },
                }
            )
        )
        monkeypatch.setattr(sidecar, "resolve_path", lambda _path: statusline)
        monkeypatch.setattr(
            sidecar, "discover_anthropic_oauth_email", lambda _path: "claude@example.com"
        )

        cards, blocked = sidecar.GenericCollector.collect_provider(
            "anthropic",
            {
                "icon": "🟠",
                "rules": [{"type": "file_json_statusline", "paths": [str(statusline)]}],
            },
        )

        assert blocked == []
        assert {card["service_name"] for card in cards} == {
            "Claude (Session Window)",
            "Claude (Weekly Window)",
        }
        assert all(card["account_id"] == "claude@example.com" for card in cards)


class TestDeepSeekCredentialRules:
    """The DeepSeek balance collector consumes the same API key the opencode
    CLI stores for its BYOK provider — both registries must ship the rule."""

    def test_baked_and_canonical_registries_discover_env_and_auth_json(self):
        registry = json.loads(
            (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
        )
        for provider in (sidecar.__REGISTRY__["providers"], registry["providers"]):
            rules = provider["deepseek"]["rules"]
            env_rule = next(rule for rule in rules if rule.get("type") == "env")
            assert env_rule["variable"] == "DEEPSEEK_API_KEY"
            assert env_rule["mapping"] == {"value": "api_key"}
            file_rule = next(rule for rule in rules if rule.get("type") == "file")
            assert "~/.local/share/opencode/auth.json" in file_rule["paths"]
            assert "~/.opencode/auth.json" in file_rule["paths"]
            assert file_rule["mapping"] == {"deepseek.key": "api_key"}


class TestOpencodeAuthJsonRuleMirrors:
    """Every sidecar rule reading opencode's auth.json must exist in registry.json.

    #340 added sibling-provider rules (openrouter / minimax / kimi_coding) to the
    sidecar's baked registry only, so the server's "single source of truth" drifted
    (#351) — same file, same mapping shape, different outcome. Providers are
    discovered from the sidecar side, so the next sibling added there cannot
    desync silently; the pinned set below additionally catches a rule being
    dropped from either registry.
    """

    _EXPECTED = {
        "openrouter",
        "minimax",
        "kimi_coding",
        "deepseek",
        "opencode",
        "ollama",
        "xai",
    }

    @staticmethod
    def _auth_json_file_rules(rules: list) -> list[dict]:
        """File rules whose paths point at opencode's auth.json (either location)."""
        return [
            rule
            for rule in rules
            if rule.get("type") == "file"
            and any("auth.json" in path and "opencode" in path for path in rule.get("paths", []))
        ]

    @classmethod
    def _sidecar_rules(cls) -> dict[str, list[dict]]:
        found: dict[str, list[dict]] = {}
        for provider_id, provider in sidecar.__REGISTRY__["providers"].items():
            rules = cls._auth_json_file_rules(provider.get("rules", []))
            if rules:
                found[provider_id] = rules
        return found

    def test_sidecar_auth_json_provider_set_is_pinned(self):
        assert set(self._sidecar_rules()) == self._EXPECTED

    def test_canonical_registry_mirrors_paths_and_mappings(self):
        registry = json.loads(
            (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
        )
        for provider_id, sidecar_rules in self._sidecar_rules().items():
            assert provider_id in registry["providers"], f"{provider_id} missing from registry.json"
            canonical_rules = self._auth_json_file_rules(
                registry["providers"][provider_id]["rules"]
            )
            assert canonical_rules, f"{provider_id}: no auth.json file rule in registry.json"
            for rule in sidecar_rules:
                match = next(
                    (
                        candidate
                        for candidate in canonical_rules
                        if set(candidate["paths"]) == set(rule["paths"])
                    ),
                    None,
                )
                assert match is not None, (
                    f"{provider_id}: canonical auth.json paths drift ({sorted(rule['paths'])})"
                )
                assert match["mapping"] == rule["mapping"], (
                    f"{provider_id}: canonical mapping drift ({rule['mapping']})"
                )

    @pytest.mark.parametrize(
        ("provider_id", "mapping"),
        [
            ("openrouter", {"openrouter.key": "api_key"}),
            ("minimax", {"minimax-coding-plan.key": "api_key"}),
            ("kimi_coding", {"kimi-code-plan-global.key": "api_key"}),
        ],
    )
    def test_sibling_providers_keep_their_auth_json_mappings(self, provider_id, mapping):
        """#351: the three sibling keys added by #340 must stay in both registries."""
        for providers in (
            sidecar.__REGISTRY__["providers"],
            json.loads((_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8"))[
                "providers"
            ],
        ):
            rules = self._auth_json_file_rules(providers[provider_id]["rules"])
            assert any(rule["mapping"] == mapping for rule in rules), provider_id
            assert any(
                set(rule["paths"]) == {"~/.local/share/opencode/auth.json", "~/.opencode/auth.json"}
                for rule in rules
            ), provider_id


class TestXaiCredentialRules:
    def test_baked_and_canonical_registries_map_actual_auth_fields(self):
        registry = json.loads(
            (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
        )
        for provider in (sidecar.__REGISTRY__["providers"], registry["providers"]):
            file_rule = next(
                rule for rule in provider["xai"]["rules"] if rule.get("type") == "file"
            )
            assert file_rule["mapping"] == {
                "xai.access": "xai_access",
                "xai.refresh": "xai_refresh",
            }

    def test_file_rule_dispatch_extracts_access_and_true_refresh_token(self, tmp_path, monkeypatch):
        auth_path = tmp_path / "auth.json"
        auth_path.write_text(
            json.dumps(
                {
                    "xai": {
                        "access": "access-bearer",
                        "refresh": "real-refresh-token",
                    }
                }
            )
        )
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [auth_path])
        cards, blocked = sidecar.GenericCollector.collect_provider(
            "xai",
            sidecar.__REGISTRY__["providers"]["xai"],
            account_label_hints={"xai": {f"path:{auth_path.resolve()}": "alice@example.com"}},
        )

        assert blocked == []
        assert cards[0]["account_id"] == "alice@example.com"
        assert cards[0]["metadata"]["xai_access"] == "access-bearer"
        assert cards[0]["metadata"]["xai_refresh"] == "real-refresh-token"


class TestFreshnessProbe:
    """The sidecar's freshness probe must accept the same expiry keys as the
    server's picker, and read a millisecond epoch as milliseconds."""

    def test_top_level_expiry_key_of_any_spelling(self, tmp_path):
        live = tmp_path / "live.json"
        live.write_text(json.dumps({"accessToken": "mmoat_live", "expiry_date": 4102444800000}))
        dead = tmp_path / "dead.json"
        dead.write_text(json.dumps({"accessToken": "mmoat_dead", "expiry_date": 1600000000000}))

        assert sidecar.has_unexpired_token(live) is True
        assert sidecar.has_unexpired_token(dead) is False


class TestAntigravityTokenStamp:
    """The agy token file carries no email, so the token card must be stamped with
    the resolved account identity (else the server hashes the refresh_token into a
    cache key that won't match the email-seeded collector → blank quota)."""

    @staticmethod
    def _ag_config(tok_path: Path) -> dict:
        return {
            "name": "Antigravity",
            "icon": "🛸",
            "rules": [
                {
                    "type": "file",
                    "paths": [str(tok_path)],
                    "format": "json",
                    "mapping": {
                        "token.access_token": "oauth_token",
                        "token.refresh_token": "refresh_token",
                    },
                }
            ],
        }

    @staticmethod
    def _write_token_file(tmp_path: Path) -> Path:
        tok = tmp_path / "antigravity-oauth-token"
        tok.write_text(
            json.dumps(
                {
                    "auth_method": "consumer",
                    "token": {"access_token": "ya29.x", "refresh_token": "1//r"},
                }
            )
        )
        return tok

    def test_token_card_does_not_inherit_server_account_hint(self, tmp_path):
        tok = self._write_token_file(tmp_path)
        config = self._ag_config(tok)
        with patch.dict(
            sidecar._ACCOUNT_IDENTITIES, {"antigravity": "user@example.com"}, clear=True
        ):
            cards, _blocked = sidecar.GenericCollector.collect_provider("antigravity", config)
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert len(token_cards) == 1
        assert token_cards[0]["account_id"] is None
        assert token_cards[0]["metadata"]["identity_pending"] is True
        assert len(_blocked) == 1

    def test_token_card_defaults_when_email_unknown(self, tmp_path):
        tok = self._write_token_file(tmp_path)
        config = self._ag_config(tok)
        with patch.dict(sidecar._ACCOUNT_IDENTITIES, {}, clear=True):
            cards, _blocked = sidecar.GenericCollector.collect_provider("antigravity", config)
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert len(token_cards) == 1
        assert token_cards[0]["account_id"] is None
        assert token_cards[0]["metadata"]["identity_pending"] is True
        assert len(_blocked) == 1


class TestChatGPTTokenStamp:
    """The registry's file rule copies auth.json's own `tokens.account_id`
    through verbatim — Codex CLI's internal OpenAI account UUID — but the
    server's durable collector identity is seeded from LatestUsage's email.
    The two never match as a token_cache key, so the collector's lookup
    silently falls back to a stale dashboard-configured cookie (see
    chatgpt_oauth.py's Priority 3). The token card must be stamped with the
    resolved email instead, overriding whatever raw account_id the file
    mapping already found.
    """

    @staticmethod
    def _codex_config(auth_path: Path) -> dict:
        return {
            "name": "ChatGPT",
            "icon": "💬",
            "rules": [
                {
                    "type": "file",
                    "paths": [str(auth_path)],
                    "format": "json",
                    "mapping": {
                        "tokens.access_token": "oauth_token",
                        "tokens.refresh_token": "refresh_token",
                        "tokens.id_token": "id_token",
                        "tokens.account_id": "account_id",
                    },
                }
            ],
        }

    def test_token_card_stamped_with_resolved_email_not_raw_uuid(self, tmp_path):
        id_token = _make_jwt({"email": "codex@example.com"})
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": "sk-live-token",
                        "refresh_token": "refresh-abc",
                        "id_token": id_token,
                        # Codex CLI's internal OpenAI account UUID — must NOT
                        # end up as the pushed account_id.
                        "account_id": "f0ab5b6d-295f-4e9e-9f4e-2ed5a8704363",
                    }
                }
            )
        )
        config = self._codex_config(auth_file)
        with patch.object(sidecar.os.path, "expanduser", return_value=str(auth_file)):
            cards, _blocked = sidecar.GenericCollector.collect_provider("chatgpt", config)

        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert len(token_cards) == 1
        assert token_cards[0]["account_id"] == "codex@example.com"
        assert token_cards[0]["account_id"] != "f0ab5b6d-295f-4e9e-9f4e-2ed5a8704363"

    def test_token_card_defaults_when_email_undecodable(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": "sk-live-token",
                        "account_id": "f0ab5b6d-295f-4e9e-9f4e-2ed5a8704363",
                    }
                }
            )
        )
        config = self._codex_config(auth_file)
        with patch.object(sidecar.os.path, "expanduser", return_value=str(auth_file)):
            cards, _blocked = sidecar.GenericCollector.collect_provider("chatgpt", config)

        # No email anywhere: the credential ships pending so the server can ask the usage
        # endpoint whose it is (#460), and the origin is reported as needing a mapping.
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert len(token_cards) == 1
        assert token_cards[0]["metadata"]["identity_pending"] is True
        assert token_cards[0]["account_id"] is None
        assert len(_blocked) == 1


class TestAntigravityTokenExpiry:
    """agy's opaque access token carries no JWT exp claim, so `expiry_date`
    (the freshness signal token_cache._is_staler needs) must be derived from
    the token file's ISO8601 `token.expiry` field.

    Without this, an expired local agy token from one sidecar silently
    clobbers a valid token pushed by another sidecar under the same cache
    key — the root cause of the "CLI shows 0% left, app shows 55% left"
    incident (Antigravity Gemini session quota).
    """

    @staticmethod
    def _write_token_file(tmp_path: Path, expiry_iso: str) -> Path:
        tok = tmp_path / "antigravity-oauth-token"
        tok.write_text(
            json.dumps(
                {
                    "auth_method": "consumer",
                    "token": {
                        "access_token": "ya29.x",
                        "refresh_token": "1//r",
                        "expiry": expiry_iso,
                    },
                }
            )
        )
        return tok

    @staticmethod
    def _config(tok_path: Path) -> dict:
        """The real production antigravity *file* rule (not a hand-rolled
        subset), so this exercises the actual `token.expiry` mapping.

        Scoped to the file rule on purpose: the provider also carries the
        Windows Credential Manager rule, and reading the host's live
        credential store would make this test depend on whether the machine
        running it has agy logged in.
        """
        config = json.loads(json.dumps(sidecar.__REGISTRY__["providers"]["antigravity"]))
        config["rules"] = [r for r in config["rules"] if r["type"] == "file"]
        config["rules"][0]["paths"] = [str(tok_path)]
        return config

    def test_valid_token_gets_expiry_date_stamped_as_ms_epoch(self, tmp_path):
        import datetime as dt

        future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()
        tok = self._write_token_file(tmp_path, future)
        config = self._config(tok)

        with patch.object(sidecar, "_ag_account_email", return_value="user@example.com"):
            cards, _blocked = sidecar.GenericCollector.collect_provider("antigravity", config)

        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert len(token_cards) == 1
        meta = token_cards[0]["metadata"]
        assert meta["oauth_token"] == "ya29.x"
        assert "expiry_date" in meta
        # ms epoch, roughly 1 hour in the future
        assert int(meta["expiry_date"]) > int(time.time() * 1000)
        assert "_raw_expiry" not in meta  # temp field must not leak into the push

    def test_expired_token_is_not_pushed(self, tmp_path):
        """A lapsed agy session must not push its dead credential at all —
        agy owns its own refresh (no client_id in the file for Runway to
        refresh with), so pushing it would just keep re-poisoning the shared
        server-side cache entry every cycle."""
        import datetime as dt

        past = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).isoformat()
        tok = self._write_token_file(tmp_path, past)
        config = self._config(tok)

        with patch.dict(sidecar._ACCOUNT_IDENTITIES, {}, clear=True):
            cards, _blocked = sidecar.GenericCollector.collect_provider("antigravity", config)

        # No oauth_token/refresh_token survives → no token card is emitted.
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert token_cards == []

    def test_nearing_expiry_logs_pre_expiry_warning_and_still_pushes(self, tmp_path, caplog):
        """While quota still flows, a token minutes from lapsing gets a WARNING
        telling the operator how to renew it — the 2026-10-02 incident had four
        silent hourly lapses."""
        import datetime as dt
        import logging

        soon = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat()
        tok = self._write_token_file(tmp_path, soon)
        config = self._config(tok)

        with caplog.at_level(logging.WARNING):
            cards, _blocked = sidecar.GenericCollector.collect_provider("antigravity", config)

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("local token expires at" in w and "--keep-alive" in w for w in warnings)
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert len(token_cards) == 1  # still pushable: it is not dead yet
        assert "expiry_date" in token_cards[0]["metadata"]

    def test_pre_expiry_warning_is_silent_when_keep_alive_owns_renewal(self, tmp_path, caplog):
        """With --keep-alive enabled the thread (and its own failure logs)
        owns renewal; the per-cycle pre-expiry nag would just be noise."""
        import datetime as dt
        import logging

        from scripts.sidecar_pkg import keep_alive

        soon = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat()
        tok = self._write_token_file(tmp_path, soon)
        config = self._config(tok)

        with (
            patch.object(keep_alive, "_enabled", True),
            caplog.at_level(logging.WARNING),
        ):
            sidecar.GenericCollector.collect_provider("antigravity", config)

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("local token expires at" in w for w in warnings)


class TestKimiApiRegistryId:
    """B3: the sidecar's embedded registry used to discover KIMI_API_KEY (and
    the kimi-auth cookie) under the legacy id "kimi", which no collector
    reads — KimiApiCollector reads token_cache/ProviderConfig under
    "kimi_api" (app/services/collectors/kimi_api.py). A sidecar-found
    KIMI_API_KEY therefore never reached the collector that uses it."""

    def test_legacy_kimi_id_is_gone(self):
        assert "kimi" not in sidecar.__REGISTRY__["providers"]

    def test_kimi_api_key_env_var_is_registered_under_kimi_api(self):
        rules = sidecar.__REGISTRY__["providers"]["kimi_api"]["rules"]
        assert len(rules) == 1
        assert rules[0] == {
            "type": "env",
            "variable": "KIMI_API_KEY",
            "mapping": {"value": "api_key"},
        }

    def test_kimi_auth_token_and_cookie_stay_covered_by_kimi_coding(self):
        """The KIMI_AUTH_TOKEN env rule and kimi-auth cookie rule dropped
        from the legacy "kimi" entry are not lost — kimi_coding already
        covers both."""
        rules = sidecar.__REGISTRY__["providers"]["kimi_coding"]["rules"]
        rule_types_vars = {(r.get("type"), r.get("variable"), r.get("name")) for r in rules}
        assert ("env", "KIMI_AUTH_TOKEN", None) in rule_types_vars
        assert ("cookie", None, "kimi-auth") in rule_types_vars

    def test_kimi_api_key_env_discovery_lands_under_kimi_api(self, monkeypatch):
        """No account hint is seeded, so the candidate blocks (Untagged) —
        the point of this test is *which* provider_id it blocks under.
        Before the fix, KIMI_API_KEY discovery reported provider_id="kimi",
        which app/services/collectors/kimi_api.py never reads."""
        monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi-key")  # pragma: allowlist secret
        cards, blocked = sidecar.GenericCollector.collect_provider(
            "kimi_api", sidecar.__REGISTRY__["providers"]["kimi_api"]
        )
        assert cards == []
        # The origin is key-scoped (#443), so a rotated key is a new origin.
        from scripts.sidecar_pkg.identity import credential_fingerprint

        fp = credential_fingerprint("sk-test-kimi-key")  # pragma: allowlist secret
        assert blocked == [
            {
                "provider_id": "kimi_api",
                "credential_origin": f"env:KIMI_API_KEY#{fp}",
                "reason": "token_withheld",
            }
        ]


class TestKimiCliCredentialGlob:
    """kimi-cli writes a per-install credential file (kimi-code-env-<hash>.json),
    so the kimi_coding file rule must glob the credentials dir — and the scrape
    loop's 'later match overwrites' semantics must make the freshest file win."""

    def _kimi_file_rule(self) -> dict:
        for rule in sidecar.__REGISTRY__["providers"]["kimi_coding"]["rules"]:
            if rule.get("type") == "file" and "kimi-code*" in (rule.get("paths") or [""])[0]:
                return rule
        raise AssertionError("kimi_coding has no kimi-code glob rule")

    def test_registry_rule_globs_credentials_dir(self):
        rule = self._kimi_file_rule()
        assert rule["paths"] == ["~/.kimi-code/credentials/kimi-code*.json"]
        assert rule["mapping"] == {
            "access_token": "cli_access_token",
            "expires_at": "cli_expires_at",
        }

    def test_registry_never_maps_refresh_token(self):
        """The kimi_coding rules must never copy the CLI's refresh token."""
        for rule in sidecar.__REGISTRY__["providers"]["kimi_coding"]["rules"]:
            targets = set(rule.get("mapping", {}).values())
            assert "refresh_token" not in targets
            assert "cli_refresh_token" not in targets

    def test_expand_glob_returns_matches_freshest_last(self, tmp_path):
        """Glob matches sort by mtime ascending so the scrape loop (which
        overwrites tokens per file) ends with the freshest credential."""
        older = tmp_path / "kimi-code-env-aaa.json"
        older.write_text(json.dumps({"access_token": "old", "expires_at": 1}))
        newer = tmp_path / "kimi-code-env-bbb.json"
        newer.write_text(json.dumps({"access_token": "new", "expires_at": 2}))
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))

        matches = sidecar.expand_file_rule_paths([str(tmp_path / "kimi-code-env-*.json")])
        assert matches == [older, newer]

    def test_valid_token_wins_over_newer_expired_file(self, tmp_path):
        """A stale-but-recently-touched file must not beat a valid token:
        expired files sort first, valid ones last (last file wins)."""
        valid = tmp_path / "kimi-code-env-valid.json"
        valid.write_text(json.dumps({"access_token": "good", "expires_at": 9999999999}))
        stale = tmp_path / "kimi-code-env-stale.json"
        stale.write_text(json.dumps({"access_token": "bad", "expires_at": 1000}))
        os.utime(valid, (1000, 1000))  # older mtime …
        os.utime(stale, (2000, 2000))  # … but stale is the recently-touched one

        matches = sidecar.expand_file_rule_paths([str(tmp_path / "kimi-code-env-*.json")])
        assert matches == [stale, valid]

    def test_expand_plain_path_exact_match(self, tmp_path):
        """Non-glob entries keep exact-match behavior; missing files are skipped."""
        existing = tmp_path / "kimi-code.json"
        existing.write_text("{}")
        assert sidecar.expand_file_rule_paths([str(existing)]) == [existing]
        assert sidecar.expand_file_rule_paths([str(tmp_path / "missing.json")]) == []

    def test_expand_mixed_plain_and_glob(self, tmp_path):
        plain = tmp_path / "kimi-code.json"
        plain.write_text("{}")
        env_file = tmp_path / "kimi-code-env-ccc.json"
        env_file.write_text("{}")
        matches = sidecar.expand_file_rule_paths(
            [str(tmp_path / "kimi-code.json"), str(tmp_path / "kimi-code-env-*.json")]
        )
        assert set(matches) == {plain, env_file}


def _make_jwt(payload: dict) -> str:
    """Build a minimal unsigned JWT with the given payload dict."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.fakesig"


class TestDecodeIdTokenEmail:
    """_decode_id_token_email: extract email from JWT payload."""

    def test_valid_jwt_returns_email(self):
        token = _make_jwt({"email": "user@example.com", "sub": "12345"})
        result = sidecar._decode_id_token_email(token)
        assert result == "user@example.com"

    def test_jwt_missing_email_returns_none(self):
        token = _make_jwt({"sub": "12345", "name": "Test User"})
        result = sidecar._decode_id_token_email(token)
        assert result is None

    def test_invalid_string_returns_none(self):
        result = sidecar._decode_id_token_email("not-a-jwt")
        assert result is None

    def test_empty_string_returns_none(self):
        result = sidecar._decode_id_token_email("")
        assert result is None

    def test_email_without_at_sign_returns_none(self):
        """A payload with a non-email 'email' field must not pass through."""
        token = _make_jwt({"email": "notanemail"})
        result = sidecar._decode_id_token_email(token)
        assert result is None


class TestGeminiAccountEmail:
    """_gemini_account_email: reads ~/.gemini/oauth_creds.json."""

    def test_returns_default_when_file_missing(self, tmp_path):
        missing = str(tmp_path / "nonexistent.json")
        with patch.object(sidecar.os.path, "expanduser", return_value=missing):
            result = sidecar._gemini_account_email()
        assert result == "default"

    def test_returns_email_from_valid_creds(self, tmp_path):
        token = _make_jwt({"email": "gemini@example.com"})
        creds_file = tmp_path / "oauth_creds.json"
        creds_file.write_text(json.dumps({"id_token": token, "access_token": "tok"}))
        with patch.object(sidecar.os.path, "expanduser", return_value=str(creds_file)):
            result = sidecar._gemini_account_email()
        assert result == "gemini@example.com"

    def test_returns_default_when_id_token_missing(self, tmp_path):
        creds_file = tmp_path / "oauth_creds.json"
        creds_file.write_text(json.dumps({"access_token": "tok"}))
        with patch.object(sidecar.os.path, "expanduser", return_value=str(creds_file)):
            result = sidecar._gemini_account_email()
        assert result == "default"


class TestCodexAccountEmail:
    """_codex_account_email: reads ~/.codex/auth.json."""

    def test_returns_default_when_file_missing(self, tmp_path):
        missing = str(tmp_path / "nonexistent.json")
        with patch.object(sidecar.os.path, "expanduser", return_value=missing):
            result = sidecar._codex_account_email()
        assert result == "default"

    def test_returns_email_from_valid_auth(self, tmp_path):
        token = _make_jwt({"email": "codex@example.com"})
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {"id_token": token, "access_token": "tok"},
                }
            )
        )
        with patch.object(sidecar.os.path, "expanduser", return_value=str(auth_file)):
            result = sidecar._codex_account_email()
        assert result == "codex@example.com"

    def test_returns_default_when_tokens_missing(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(json.dumps({"auth_mode": "chatgpt"}))
        with patch.object(sidecar.os.path, "expanduser", return_value=str(auth_file)):
            result = sidecar._codex_account_email()
        assert result == "default"


def _expired_xai_jwt() -> str:
    import base64 as _b64

    def seg(obj):
        return _b64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'exp': int(time.time()) - 3600})}.sig"


class TestXaiExpiredLoginWarning:
    """An expired, rollable xAI login nudges the operator toward --keep-alive."""

    def _collect(self, tmp_path, monkeypatch):
        auth_path = tmp_path / "auth.json"
        auth_path.write_text(
            json.dumps({"xai": {"access": _expired_xai_jwt(), "refresh": "r", "expires": 1}})
        )
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [auth_path])
        sidecar.GenericCollector.collect_provider("xai", sidecar.__REGISTRY__["providers"]["xai"])

    def test_warns_when_keep_alive_is_off(self, tmp_path, monkeypatch, caplog):
        from scripts.sidecar_pkg import keep_alive

        monkeypatch.setattr(keep_alive, "_enabled", False)
        with caplog.at_level("WARNING"):
            self._collect(tmp_path, monkeypatch)
        assert "--keep-alive" in caplog.text

    def test_quiet_when_keep_alive_is_on(self, tmp_path, monkeypatch, caplog):
        from scripts.sidecar_pkg import keep_alive

        monkeypatch.setattr(keep_alive, "_enabled", True)
        with caplog.at_level("WARNING"):
            self._collect(tmp_path, monkeypatch)
        assert "--keep-alive" not in caplog.text


class TestCodexExpiredLoginWarning:
    """An expired Codex login nudges the operator toward --keep-alive (the token is still pushed)."""

    def _collect(self, tmp_path, monkeypatch, *, expired=True):
        auth_path = tmp_path / "auth.json"
        offset = -3600 if expired else 5 * 86400

        def seg(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

        jwt = f"{seg({'alg': 'none'})}.{seg({'exp': int(time.time()) + offset})}.sig"
        auth_path.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {"access_token": jwt, "refresh_token": "r", "account_id": "a@x.io"},
                }
            )
        )
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [auth_path])
        sidecar.GenericCollector.collect_provider(
            "chatgpt", sidecar.__REGISTRY__["providers"]["chatgpt"]
        )

    def test_warns_when_keep_alive_is_off(self, tmp_path, monkeypatch, caplog):
        from scripts.sidecar_pkg import keep_alive

        monkeypatch.setattr(keep_alive, "_enabled", False)
        with caplog.at_level("WARNING"):
            self._collect(tmp_path, monkeypatch)
        assert "Codex" in caplog.text and "--keep-alive" in caplog.text

    def test_quiet_when_keep_alive_is_on_or_the_login_is_valid(self, tmp_path, monkeypatch, caplog):
        from scripts.sidecar_pkg import keep_alive

        monkeypatch.setattr(keep_alive, "_enabled", True)
        with caplog.at_level("WARNING"):
            self._collect(tmp_path, monkeypatch)
        assert "--keep-alive" not in caplog.text
        monkeypatch.setattr(keep_alive, "_enabled", False)
        with caplog.at_level("WARNING"):
            self._collect(tmp_path, monkeypatch, expired=False)
        assert "--keep-alive" not in caplog.text
