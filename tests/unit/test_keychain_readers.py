"""Background readers use noninteractive exact-item IO without interactive CLI fallbacks."""

import json
from unittest.mock import Mock

import pytest

from scripts.sidecar_pkg import claude_code_keychain as code
from scripts.sidecar_pkg import keychain_client as client
from scripts.sidecar_pkg import macos_cookies as cookies
from scripts.sidecar_pkg import macos_keychain_store as keychain
from scripts.sidecar_pkg.macos_keychain_store import KeychainError
from tests.macos_cookie_fixtures import approved_keychain_readers  # noqa: F401


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(client, "_CLIENT", client.KeychainClient(command=["never-run"]))
    monkeypatch.setattr(cookies, "_KEY_CACHE", {})
    monkeypatch.setattr(cookies, "_KEY_STATUS", {})


def test_code_discovery_uses_exact_account_and_never_exports_refresh(monkeypatch):
    calls = []

    def read(service, account=None, **kwargs):
        calls.append((service, account))
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "synthetic-access",
                    "refreshToken": "synthetic-private",
                    "expiresAt": 4102444800000,
                }
            }
        ).encode()

    monkeypatch.setattr(keychain, "read_secret", read, raising=False)
    monkeypatch.setattr(code, "account_name", lambda: "synthetic-user")
    result = code.read_candidates(
        [],
        {"claudeAiOauth.accessToken": "oauth_token", "claudeAiOauth.refreshToken": "refresh_token"},
    )
    assert result[0][0] == {"oauth_token": "synthetic-access", "oauth_read_only": "1"}
    assert calls == [("Claude Code-credentials", "synthetic-user")]


def test_code_denial_blocks_renewal_and_later_discovery(monkeypatch):
    lookup = Mock(side_effect=KeychainError("denied"))
    monkeypatch.setattr(keychain, "read_secret", lookup, raising=False)
    assert code.read_candidates([], {}) == []
    assert code.access_blocked()
    assert client.get_keychain_client().access_blocked("claude-code")
    assert code.read_candidates([], {}) == []
    assert lookup.call_count == 1


def test_storage_denial_preserves_other_source_access(monkeypatch):
    def read(service, account=None, **kwargs):
        if service == "Chrome Safe Storage":
            raise KeychainError("denied")
        return b"synthetic-key"

    monkeypatch.setattr(keychain, "read_secret", read, raising=False)
    assert cookies.storage_key("Chrome Safe Storage") is None
    assert cookies.storage_key("Claude Safe Storage")
    assert not code.access_blocked()


@pytest.mark.parametrize(
    "service", ["Claude Code-credentials", "Claude Code-credentials-synthetic"]
)
def test_legacy_code_rules_use_same_suppression_group(monkeypatch, service):
    groups = []

    class Store:
        def __init__(self, **kwargs):
            groups.append(kwargs["group"])

        def read_secret(self, *args):
            return b"synthetic"

    monkeypatch.setattr(keychain, "KeychainStore", Store)
    assert keychain.read_secret(service, group="registry:" + service) == b"synthetic"
    assert groups == ["claude-code"]
