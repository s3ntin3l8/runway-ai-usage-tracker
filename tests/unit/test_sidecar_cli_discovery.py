"""Unit tests for sidecar local CLI paths, config dirs, and Grok auth discovery."""

from __future__ import annotations

import base64
import json
import os
import sys
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


class TestOpencodeDbPath:
    """_discover_opencode_db_path: tries the XDG path first, then ~/.opencode."""

    def _candidates(self, monkeypatch, *paths: str) -> None:
        """Map sequential calls to os.path.expanduser to the given paths."""
        iter_paths = iter(paths)
        monkeypatch.setattr(sidecar.os.path, "expanduser", lambda _: next(iter_paths))

    def test_returns_none_when_no_candidate_exists(self, tmp_path, monkeypatch):
        missing_a = str(tmp_path / "xdg" / "opencode.db")
        missing_b = str(tmp_path / "flat" / "opencode.db")
        self._candidates(monkeypatch, missing_a, missing_b)
        assert sidecar._discover_opencode_db_path() is None

    def test_prefers_xdg_path_when_both_exist(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg" / "opencode.db"
        xdg.parent.mkdir(parents=True)
        xdg.touch()
        flat = tmp_path / "flat" / "opencode.db"
        flat.parent.mkdir(parents=True)
        flat.touch()
        self._candidates(monkeypatch, str(xdg), str(flat))
        assert sidecar._discover_opencode_db_path() == xdg

    def test_falls_back_to_flat_path_when_xdg_missing(self, tmp_path, monkeypatch):
        missing_xdg = str(tmp_path / "xdg" / "opencode.db")
        flat = tmp_path / "opencode" / "opencode.db"
        flat.parent.mkdir(parents=True)
        flat.touch()
        self._candidates(monkeypatch, missing_xdg, str(flat))
        assert sidecar._discover_opencode_db_path() == flat


# ---------------------------------------------------------------------------
# _post_credential_manifest (PR #288 / #290)
# ---------------------------------------------------------------------------


def _seed_grok_auth_json(home: Path, *, block: dict | None = None) -> Path:
    grok_home = home / ".grok"
    grok_home.mkdir(parents=True, exist_ok=True)
    auth_path = grok_home / "auth.json"
    default_block = {
        "key": "grok-bearer-jwt",
        "refresh_token": "refresh-xyz",
        "expires_at": 9999999999,
        "email": "user@example.com",
        "team_id": "team-42",
        "user_id": "user-42",
        "first_name": "Ada",
        "last_name": "Lovelace",
    }
    auth_path.write_text(
        json.dumps({"https://auth.x.ai::abc123": block if block is not None else default_block})
    )
    return auth_path


def test_xai_grok_auth_card_and_events_share_email_first_identity(monkeypatch, tmp_path):
    auth_path = _seed_grok_auth_json(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(tmp_path / ".grok"))
    monkeypatch.delenv("GROK_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [auth_path])

    with patch.dict(sidecar._ACCOUNT_IDENTITIES, {}, clear=True):
        cards, blocked = sidecar.GenericCollector.collect_provider(
            "xai", sidecar.__REGISTRY__["providers"]["xai"]
        )

    assert blocked == []
    card = next(card for card in cards if card["unit"] == "oauth")
    assert card["account_id"] == sidecar._grok_account_identity() == "user@example.com"
    assert sidecar._LEGACY_EVENT_ACCOUNT_DISCOVERY["xai"]() == card["account_id"]
    assert card["account_id"] != "team-42"
    assert card["metadata"]["xai_access"] == "grok-bearer-jwt"
    assert card["metadata"]["xai_refresh"] == "refresh-xyz"
    assert card["account_label"] == "Ada Lovelace"


def test_xai_grok_uses_user_id_then_operator_tag_when_email_missing(monkeypatch, tmp_path):
    user_auth = _seed_grok_auth_json(tmp_path, block={"key": "token", "user_id": "user-42"})
    monkeypatch.setenv("GROK_HOME", str(tmp_path / ".grok"))
    monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [user_auth])

    with patch.dict(sidecar._ACCOUNT_IDENTITIES, {}, clear=True):
        cards, blocked = sidecar.GenericCollector.collect_provider(
            "xai", sidecar.__REGISTRY__["providers"]["xai"]
        )
    assert blocked == []
    assert next(card for card in cards if card["unit"] == "oauth")["account_id"] == "user-42"
    assert sidecar._grok_account_identity() == "user-42"

    untagged_auth = _seed_grok_auth_json(tmp_path, block={"key": "token", "team_id": "team-99"})
    monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [untagged_auth])
    with patch.dict(sidecar._ACCOUNT_IDENTITIES, {}, clear=True):
        cards, blocked = sidecar.GenericCollector.collect_provider(
            "xai", sidecar.__REGISTRY__["providers"]["xai"]
        )
    # xai is key-scoped (#349): the blocked origin names the credential,
    # not just the file it was found in.
    from scripts.sidecar_pkg.identity import credential_fingerprint

    assert cards == []
    assert blocked == [
        {
            "provider_id": "xai",
            "credential_origin": f"path:{untagged_auth.resolve()}"
            f"#{credential_fingerprint('token')}",
            "reason": "token_withheld",
        }
    ]

    cards, blocked = sidecar.GenericCollector.collect_provider(
        "xai",
        sidecar.__REGISTRY__["providers"]["xai"],
        account_label_hints={"xai": {f"path:{untagged_auth.resolve()}": "operator-account"}},
    )
    assert blocked == []
    assert (
        next(card for card in cards if card["unit"] == "oauth")["account_id"] == "operator-account"
    )
    assert sidecar._grok_account_identity() is None


def test_grok_refresh_only_scope_does_not_register_credential(monkeypatch, tmp_path):
    auth_path = _seed_grok_auth_json(
        tmp_path,
        block={"refresh_token": "refresh-only", "email": "user@example.com"},
    )
    monkeypatch.setenv("GROK_HOME", str(tmp_path / ".grok"))
    monkeypatch.delenv("GROK_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [auth_path])

    cards, blocked = sidecar.GenericCollector.collect_provider(
        "xai", sidecar.__REGISTRY__["providers"]["xai"]
    )

    assert cards == []
    assert blocked == []


def test_grok_identity_prefers_email_across_multiple_scope_entries():
    data = {
        "https://auth.x.ai::user-scope": {"key": "user-token", "user_id": "user-42"},
        "https://auth.x.ai::email-scope": {"key": "email-token", "email": "ada@example.com"},
    }

    assert sidecar._grok_auth_scope_entry(data)["key"] == "email-token"
    assert sidecar._grok_account_identity(data) == "ada@example.com"


def test_grok_env_bearer_is_collected_through_dispatcher(monkeypatch):
    monkeypatch.setenv("GROK_OAUTH_TOKEN", "manually-configured-bearer")
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [])
    cards, blocked = sidecar.GenericCollector.collect_provider(
        "xai",
        sidecar.__REGISTRY__["providers"]["xai"],
        account_label_hints={"xai": {"env:GROK_OAUTH_TOKEN": "tagged-account"}},
    )
    assert blocked == []
    card = next(card for card in cards if card["unit"] == "oauth")
    assert card["metadata"]["xai_access"] == "manually-configured-bearer"
    assert "xai_refresh" not in card["metadata"]
    assert card["account_id"] == "tagged-account"


def test_grok_registries_register_auth_sources_and_no_unused_oauth_parser():
    registry = json.loads(
        (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
    )
    for provider in (sidecar.__REGISTRY__["providers"], registry["providers"]):
        rules = provider["xai"]["rules"]
        file_rule = next(rule for rule in rules if rule.get("type") == "file")
        assert file_rule["mapping"] == {"xai.access": "xai_access", "xai.refresh": "xai_refresh"}
        assert any(rule.get("type") == "xai_grok_cli_auth" for rule in rules)
        env_rule = next(rule for rule in rules if rule.get("type") == "env")
        assert env_rule["variable"] == "GROK_OAUTH_TOKEN"
        assert env_rule["mapping"]["value"] == "xai_access"
        assert all(rule.get("parser") != "xai_oauth" for rule in rules)


def test_grok_updates_discovery_includes_updates_jsonl_not_signals(monkeypatch, tmp_path):
    grok_home = tmp_path / ".grok"
    session = grok_home / "sessions" / "%2Fhome%2Fuser%2Fproj" / "sess1"
    session.mkdir(parents=True)
    (session / "updates.jsonl").write_text("{}\n")
    (session / "signals.json").write_text('{"totalTokensBeforeCompaction": 999}')
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    assert sidecar._discover_grok_updates_paths() == [session / "updates.jsonl"]


def test_grok_account_extractor_uses_xai_watermark_and_bootstrap_window(tmp_path):
    import datetime as dt

    from scripts.sidecar_pkg.event_extractors.xai import parse_xai_events

    updates = tmp_path / "%2Fhome%2Fuser%2Fproj" / "sess1" / "updates.jsonl"
    updates.parent.mkdir(parents=True)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    old_turn = now - dt.timedelta(days=60)
    recent_turn = now - dt.timedelta(seconds=1)

    def envelope(timestamp: dt.datetime, prompt_id: str) -> dict:
        return {
            "timestamp": timestamp.isoformat(),
            "method": "session/update",
            "params": {
                "sessionId": "sess1",
                "update": {
                    "sessionUpdate": "turn_completed",
                    "prompt_id": prompt_id,
                    "usage": {"inputTokens": 10},
                },
            },
        }

    updates.write_text(
        "\n".join(
            json.dumps(row) for row in [envelope(old_turn, "old"), envelope(recent_turn, "recent")]
        )
    )
    extractor = sidecar._make_account_extractor(parse_xai_events, lambda: [updates])

    class BootstrapWatermark:
        def last_pushed(self, provider_id, account_id):
            assert provider_id == "xai"
            assert account_id == "alice@example.com"
            return

    assert [
        event.event_id for event in extractor("alice@example.com", BootstrapWatermark(), 90)
    ] == [
        "xai|grok|sess1|old|unknown",
        "xai|grok|sess1|recent|unknown",
    ]
    assert [
        event.event_id for event in extractor("alice@example.com", BootstrapWatermark(), 30)
    ] == ["xai|grok|sess1|recent|unknown"]

    class ReplayingWatermark:
        def last_pushed(self, provider_id, account_id):
            assert provider_id == "xai"
            return recent_turn

    replay = extractor("alice@example.com", ReplayingWatermark(), 0)
    assert [event.event_id for event in replay] == ["xai|grok|sess1|recent|unknown"]


# ---------------------------------------------------------------------------
# Hermes Agent session extraction wiring and canonical hints
# ---------------------------------------------------------------------------


def test_grok_scope_selection_is_shared_with_the_renewer():
    from scripts.sidecar_pkg import xai_renewer

    data = {
        "https://auth.x.ai::a": {"key": "ka", "refresh_token": "ra"},
        "https://auth.x.ai::b": {"key": "kb", "refresh_token": "rb", "email": "me@x.ai"},
        "unrelated": {"key": "z"},
    }
    assert sidecar._grok_auth_scope_entry(data) is xai_renewer.grok_scope_entry(data)
    assert sidecar._grok_auth_scope_entry(data)["key"] == "kb"
    assert sidecar._grok_auth_scope_entry("nope") is None


class TestCustomLoginDirs:
    """CLAUDE_CONFIG_DIR / CODEX_HOME relocate a CLI's whole state dir (#525)."""

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setattr(sidecar, "_LOGIN_DIR_EXTRAS", {})

    @staticmethod
    def _claude_login(directory: Path, email: str) -> Path:
        directory.mkdir(parents=True)
        creds = directory / ".credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
        (directory / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": email}})
        )
        return creds

    def test_unset_env_adds_no_paths(self):
        assert sidecar.custom_login_dirs("CLAUDE_CONFIG_DIR") == []
        assert sidecar.resolve_paths("{{ENV_DIRS:CLAUDE_CONFIG_DIR}}/.credentials.json") == []
        assert sidecar.resolve_paths("~/.claude/x.json") == [
            sidecar.resolve_path("~/.claude/x.json")
        ]

    def test_env_dir_is_discovered_and_paired_with_its_own_claude_json(self, monkeypatch, tmp_path):
        creds = self._claude_login(tmp_path / "work", "work@example.com")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work"))

        found = sidecar.expand_file_rule_paths(["{{ENV_DIRS:CLAUDE_CONFIG_DIR}}/.credentials.json"])

        assert found == [creds]
        assert sidecar.discover_anthropic_oauth_email(creds) == "work@example.com"
        assert sidecar.discover_anthropic_email() == "work@example.com"

    def test_multiple_dirs_pair_to_distinct_accounts(self, monkeypatch, tmp_path):
        a = self._claude_login(tmp_path / "a", "a@example.com")
        b = self._claude_login(tmp_path / "b", "b@example.com")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{tmp_path / 'a'}, {tmp_path / 'b'}")

        assert sidecar.discover_anthropic_oauth_email(a) == "a@example.com"
        assert sidecar.discover_anthropic_oauth_email(b) == "b@example.com"

    def test_unrelated_credentials_file_is_not_paired(self, tmp_path):
        stray = self._claude_login(tmp_path / "stray", "stray@example.com")
        assert sidecar.discover_anthropic_oauth_email(stray) == ""

    def test_config_key_covers_service_without_env(self, tmp_path):
        creds = self._claude_login(tmp_path / "svc", "svc@example.com")
        sidecar.configure_login_dirs({"claude_config_dirs": [str(tmp_path / "svc")]})

        assert sidecar.expand_file_rule_paths(
            ["{{ENV_DIRS:CLAUDE_CONFIG_DIR}}/.credentials.json"]
        ) == [creds]
        assert sidecar.discover_anthropic_oauth_email(creds) == "svc@example.com"

    def test_codex_home_login_email_and_sessions(self, monkeypatch, tmp_path):
        home = tmp_path / "codex"
        (home / "sessions" / "2026").mkdir(parents=True)
        payload = base64.urlsafe_b64encode(
            json.dumps({"email": "cx@example.com"}).encode()
        ).decode()
        (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"h.{payload}.s"}}))
        log = home / "sessions" / "2026" / "rollout.jsonl"
        log.write_text("{}\n")
        monkeypatch.setenv("CODEX_HOME", str(home))

        assert sidecar.expand_file_rule_paths(["{{ENV_DIRS:CODEX_HOME}}/auth.json"]) == [
            home / "auth.json"
        ]
        assert sidecar._codex_account_email() == "cx@example.com"
        assert log in sidecar._discover_codex_log_paths()

    def test_claude_log_paths_accept_dir_or_projects_dir(self, monkeypatch, tmp_path):
        log = tmp_path / "cfg" / "projects" / "p" / "s.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("{}\n")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        assert sidecar._discover_anthropic_log_paths() == [log]
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg" / "projects"))
        assert sidecar._discover_anthropic_log_paths() == [log]

    def test_both_registries_list_the_env_dir_paths(self):
        registry = json.loads(
            (_REPO_ROOT / "app" / "core" / "registry.json").read_text(encoding="utf-8")
        )
        for providers in (sidecar.__REGISTRY__["providers"], registry["providers"]):
            claude = [p for r in providers["anthropic"]["rules"] for p in r.get("paths", [])]
            codex = [p for r in providers["chatgpt"]["rules"] for p in r.get("paths", [])]
            assert "{{ENV_DIRS:CLAUDE_CONFIG_DIR}}/.credentials.json" in claude
            assert "{{ENV_DIRS:CLAUDE_CONFIG_DIR}}/statusline.json" in claude
            assert "{{ENV_DIRS:CODEX_HOME}}/auth.json" in codex

    def test_env_dir_equal_to_standard_dir_is_not_listed_twice(self, monkeypatch):
        creds = self._claude_login(Path(os.path.expanduser("~/.claude")), "me@example.com")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))

        found = sidecar.expand_file_rule_paths(
            ["~/.claude/.credentials.json", "{{ENV_DIRS:CLAUDE_CONFIG_DIR}}/.credentials.json"]
        )

        assert found == [creds]


class TestOpencodeDbOverride:
    """OPENCODE_DB (opencode v2's override) takes precedence over the defaults."""

    def test_absolute_override_wins(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom.db"
        custom.touch()
        monkeypatch.setenv("OPENCODE_DB", str(custom))
        assert sidecar._discover_opencode_db_path() == custom

    def test_missing_override_does_not_fall_back_to_defaults(self, tmp_path, monkeypatch):
        # A default DB exists, but an explicit (typo'd) override must be loud, not silent.
        default = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
        default.parent.mkdir(parents=True)
        default.touch()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser on Windows
        monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "typo.db"))
        assert sidecar._discover_opencode_db_path() is None

    def test_relative_override_resolves_under_data_dir(self, tmp_path, monkeypatch):
        custom = tmp_path / ".local" / "share" / "opencode" / "custom.db"
        custom.parent.mkdir(parents=True)
        custom.touch()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser on Windows
        monkeypatch.setenv("OPENCODE_DB", "custom.db")
        assert sidecar._discover_opencode_db_path() == custom

    def test_memory_override_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser on Windows
        monkeypatch.setenv("OPENCODE_DB", ":memory:")
        assert sidecar._discover_opencode_db_path() is None
