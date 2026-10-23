"""Synthetic Keychain consent tests: no frameworks or real credentials."""

import pytest

from scripts.sidecar_pkg.macos_keychain_store import _WorkerAPI
from scripts.sidecar_pkg.source_catalog import catalog
from scripts.sidecar_pkg.source_consent import SourcePolicy

WORKER_CALL = _WorkerAPI.call


def test_catalog_is_static_and_includes_keychain_dependencies():
    sources = catalog()
    assert sources["anthropic.claude-code"].services == ("Claude Code-credentials",)
    assert sources["anthropic.chrome"].services == ("Chrome Safe Storage",)
    assert sources["anthropic.desktop-oauth"].renewal is False
    assert not any("file" in key for key in sources)


@pytest.mark.parametrize("value", [None, 1, "true", [], {}, False])
def test_exact_true_default_off(value):
    policy = SourcePolicy(
        {"keychain_sources": {"anthropic.claude-code": {"read": value, "revision": 1}}}
    )
    assert policy.ticket("anthropic.claude-code") is None


def test_bulk_is_snapshot_and_never_enables_writes():
    config = SourcePolicy.enable_all({})
    policy = SourcePolicy(config)
    assert policy.ticket("anthropic.chrome")
    assert not policy.renewal_allowed()
    config["keychain_sources"]["future.unknown"] = {"read": True, "revision": 1}
    assert SourcePolicy(config).ticket("future.unknown") is None


def test_disable_and_reenable_cannot_revive_old_ticket():
    config = SourcePolicy.enable_all({})
    policy = SourcePolicy(config)
    ticket = policy.ticket("anthropic.claude-code")
    assert policy.current(ticket)
    off = SourcePolicy.change(config, "anthropic.claude-code", read=False)
    policy.configure(off)
    assert not policy.current(ticket)
    policy.configure(SourcePolicy.change(off, "anthropic.claude-code", read=True))
    assert not policy.current(ticket)
    assert policy.current(policy.ticket("anthropic.claude-code"))


def test_separate_renewal_gate_and_remote_pause():
    config = SourcePolicy.enable_all({})
    policy = SourcePolicy(config)
    assert not policy.renewal_allowed()
    config["claude_keychain_renewal"] = True
    policy.configure(config)
    assert policy.renewal_allowed()
    policy.pause("anthropic.claude-code", True)
    assert not policy.renewal_allowed()
    policy.pause("anthropic.claude-code", False)
    policy.configure({})
    assert policy.ticket("anthropic.claude-code") is None


def test_destination_change_invalidates_consent():
    config = SourcePolicy.enable_all({"api_url": "https://first.example"})
    policy = SourcePolicy(config)
    ticket = policy.ticket("anthropic.chrome")
    policy.configure({**config, "api_url": "https://second.example"})
    assert not policy.current(ticket)
    assert policy.ticket("anthropic.chrome") is None


def test_policy_rejects_new_capabilities_and_unknown_changes():
    config = SourcePolicy.enable_all({})
    config["keychain_sources"]["anthropic.chrome"]["revision"] = 999
    assert SourcePolicy(config).ticket("anthropic.chrome") is None
    with pytest.raises(ValueError, match="Unknown scraper"):
        SourcePolicy.change(config, "unknown", read=True)


def test_disabled_keychain_read_cannot_start_worker(monkeypatch):
    from scripts.sidecar_pkg import keychain_client
    from scripts.sidecar_pkg import macos_keychain_store as store
    from scripts.sidecar_pkg import source_consent as consent

    monkeypatch.setattr(consent, "POLICY", SourcePolicy({}))
    monkeypatch.setattr(
        keychain_client,
        "get_keychain_client",
        lambda: pytest.fail("disabled source started worker"),
    )
    with pytest.raises(store.KeychainError, match="permission disabled"):
        WORKER_CALL(store._WorkerAPI(), "copy", {"service": "Claude Code-credentials"}, {})


def test_disabled_cached_browser_key_is_not_reused(monkeypatch):
    from scripts.sidecar_pkg import macos_cookies
    from scripts.sidecar_pkg import source_consent as consent

    monkeypatch.setattr(consent, "POLICY", SourcePolicy({}))
    monkeypatch.setattr(macos_cookies, "_KEY_CACHE", {"Chrome Safe Storage": b"synthetic"})
    assert macos_cookies.storage_key("Chrome Safe Storage") is None


def test_remote_rule_cannot_broaden_browser_resource(monkeypatch):
    from scripts.sidecar_pkg import source_consent as consent
    from scripts.sidecar_pkg.source_catalog_data import KEYCHAIN_RULES

    monkeypatch.setattr(consent, "POLICY", SourcePolicy(SourcePolicy.enable_all({})))
    original = next(r for r in KEYCHAIN_RULES["anthropic"]["rules"] if r["type"] == "cookie")
    with consent.rule_scope("anthropic", original):
        assert consent.service_ticket("Chrome Safe Storage")
        assert consent.cookie_ticket("Chrome Safe Storage", "claude.ai", "sessionKey")
        assert not consent.cookie_ticket("Chrome Safe Storage", "evil.example", "sessionKey")
    with consent.rule_scope("anthropic", {**original, "domains": ["evil.example"]}):
        assert not consent.service_ticket("Chrome Safe Storage")


def test_desktop_off_blocks_before_reading_config(monkeypatch, tmp_path):
    from scripts.sidecar_pkg import claude_desktop
    from scripts.sidecar_pkg import source_consent as consent

    monkeypatch.setattr(consent, "POLICY", SourcePolicy({}))
    monkeypatch.setattr(
        type(tmp_path), "read_text", lambda *a, **k: pytest.fail("off read Desktop storage")
    )
    assert claude_desktop.read_oauth_candidates(tmp_path / "config.json") == []


def test_config_is_atomically_owner_only(tmp_path):
    import json
    import stat
    import sys

    from sidecar_app.config import save_local_config

    target = tmp_path / "config.json"
    save_local_config(target, {"keychain_sources": {"test": False}})
    assert json.loads(target.read_text()) == {"keychain_sources": {"test": False}}
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [target]


def test_mac_github_fallback_never_launches_cli(monkeypatch):
    from scripts import sidecar
    from scripts.sidecar_pkg import source_consent as consent
    from scripts.sidecar_pkg.source_catalog_data import KEYCHAIN_RULES

    monkeypatch.setattr(consent, "POLICY", SourcePolicy({}))
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        sidecar.subprocess, "run", lambda *a, **k: pytest.fail("must not launch gh/security")
    )
    rule = next(r for r in KEYCHAIN_RULES["github"]["rules"] if r["type"] == "exec")
    cards, _ = sidecar.GenericCollector.collect_provider("github", {"rules": [rule]})
    assert cards == []


def test_fleet_directories_do_not_expand_keychain_targets(monkeypatch, tmp_path):
    from scripts import sidecar

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        sidecar, "_LOGIN_DIR_EXTRAS", {"CLAUDE_CONFIG_DIR": [str(tmp_path / "local")]}
    )
    monkeypatch.setattr(
        sidecar, "_LOGIN_DIR_REMOTE", {"claude_config_dirs": [str(tmp_path / "remote")]}
    )
    assert sidecar.local_keychain_login_dirs() == [tmp_path / "local"]


def test_keychain_egress_is_withheld_until_withdrawal_protocol(monkeypatch):
    from scripts.sidecar_pkg import source_consent as consent

    monkeypatch.setattr(consent.sys, "platform", "darwin")
    from scripts.sidecar_pkg.source_consent import filter_egress

    keychain = {
        "metadata": {
            "provider_id": "anthropic",
            "credential_origin": "keychain:Claude Code-credentials",
            "oauth_token": "synthetic-private",
        }
    }
    file = {
        "metadata": {
            "provider_id": "anthropic",
            "credential_origin": "path:/synthetic/file",
            "oauth_token": "synthetic-file",
        }
    }
    assert filter_egress({"metrics": [keychain, file]})["metrics"] == [file]


@pytest.mark.parametrize("reason", ["source_locked", "source_unsupported", "source_unreadable"])
def test_a_blocked_source_report_survives_the_macOS_keychain_gate(monkeypatch, reason):
    """A store the machine could not read is a report, not keychain material.

    On macOS every browser-cookie origin counts as a native-browser source, so
    the consent gate would drop this report - and the provider would look like
    nobody is signed into it. The report names the store and the fix, nothing
    that was read out of the Keychain.
    """
    from scripts.sidecar_pkg import source_consent as consent

    monkeypatch.setattr(consent.sys, "platform", "darwin")
    from scripts.sidecar_pkg.source_consent import filter_egress

    blocked = {
        "provider_id": "ollama",
        "credential_origin": "cookie:ollama/Chrome/Default/Network/Cookies",
        "reason": reason,
        "source_store": "Chrome/Default/Network/Cookies",
        "source_detail": "held open by another process",
        "source_remedy": "set OLLAMA_SESSION_TOKEN (or paste the cookie in Settings → Providers)",
    }
    assert filter_egress({"entries": [blocked]})["entries"] == [blocked]


def test_a_withheld_token_stays_gated_by_the_macOS_keychain_rule(monkeypatch):
    """Only a source report is exempt; a withheld cookie origin is still gated."""
    from scripts.sidecar_pkg import source_consent as consent

    monkeypatch.setattr(consent.sys, "platform", "darwin")
    from scripts.sidecar_pkg.source_consent import filter_egress

    withheld = {
        "provider_id": "anthropic",
        "credential_origin": "cookie:anthropic/Chrome/Profile 1/Cookies",
        "reason": "token_withheld",
    }
    assert filter_egress({"entries": [withheld]})["entries"] == []


def test_withheld_native_scan_is_not_authoritative_logout(monkeypatch):
    from scripts.sidecar_pkg import source_consent as consent

    monkeypatch.setattr(consent.sys, "platform", "darwin")
    payload = {
        "entries": [],
        "observations": [],
        "completed_providers": ["anthropic", "openrouter"],
    }
    assert consent.filter_egress(payload)["completed_providers"] == ["openrouter"]


def test_windows_lifecycle_catalog_never_expands_keychain_opt_ins():
    from scripts.sidecar_pkg.source_catalog import control_catalog

    windows = {"github.windows-credential", "antigravity.windows-credential"}
    assert windows <= control_catalog().keys()
    assert windows.isdisjoint(catalog())
    enabled = SourcePolicy.enable_all({})
    assert windows.isdisjoint(enabled["keychain_sources"])
    for source_id in windows:
        with pytest.raises(ValueError, match="Unknown scraper"):
            SourcePolicy.change(enabled, source_id, read=True)
