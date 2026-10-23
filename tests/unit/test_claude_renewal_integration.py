"""Local permission cannot be granted by remote keep-alive settings."""

from scripts import sidecar
from scripts.sidecar_pkg import claude_keychain_renewer as renewal


def test_factory_composes_file_and_keychain_renewers():
    renewers = sidecar._make_keep_alive_renewers()
    claude = [r for r in renewers if r.name == "anthropic"]
    assert len(claude) == 1
    assert isinstance(claude[0].keychain, renewal.KeychainRenewer)


def test_live_local_permission_reads_replaced_config(monkeypatch):
    monkeypatch.setattr(sidecar, "_CLAUDE_KEYCHAIN_PERMISSION", lambda: False)
    runner = sidecar.DaemonRunner({"api_url": "https://example.test", "api_key": "synthetic"})
    assert not sidecar._claude_keychain_renewal_enabled()
    runner._config = {"claude_keychain_renewal": "true"}
    assert not sidecar._claude_keychain_renewal_enabled()
    runner._config = {"claude_keychain_renewal": True}
    assert not sidecar._claude_keychain_renewal_enabled()
    from scripts.sidecar_pkg.source_consent import POLICY, SourcePolicy

    runner._config = SourcePolicy.enable_all({"claude_keychain_renewal": True})
    POLICY.configure(runner._config)
    assert sidecar._claude_keychain_renewal_enabled()
    runner._config = {}
    assert not sidecar._claude_keychain_renewal_enabled()
