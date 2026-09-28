"""Unit tests for the sidecar TLS trust-store helper.

Covers scripts/sidecar_pkg/tls.build_context and the sidecar.build_ssl_context
wrapper that resolves a verifying SSL context for HTTPS pushes — the fix for the
macOS `CERTIFICATE_VERIFY_FAILED` failure against a valid public cert.
"""

import ssl
import sys
from pathlib import Path

import certifi
import pytest

import scripts.sidecar_pkg.tls as tls
from scripts.sidecar_pkg.self_update import _github_ssl_context

# Import sidecar as a module (it lives in scripts/, not a package)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import sidecar  # noqa: E402


def test_http_url_returns_no_context():
    assert tls.build_context("http://server:8765") is None
    assert sidecar.build_ssl_context("http://server:8765") is None


def test_https_default_verifies():
    ctx = sidecar.build_ssl_context("https://server")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_config_tls_insecure_disables_verification():
    ctx = sidecar.build_ssl_context("https://server", {"tls_insecure": True})
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_env_insecure_disables_verification(monkeypatch):
    monkeypatch.setenv("RUNWAY_INSECURE", "1")
    ctx = sidecar.build_ssl_context("https://server")
    assert ctx.verify_mode == ssl.CERT_NONE


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_env_insecure_falsey_keeps_verification(monkeypatch, value):
    monkeypatch.setenv("RUNWAY_INSECURE", value)
    ctx = sidecar.build_ssl_context("https://server")
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_explicit_ca_bundle_is_honoured():
    # certifi's own bundle stands in for a custom CA PEM that exists on disk.
    ctx = sidecar.build_ssl_context("https://server", {"ca_bundle": certifi.where()})
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_missing_ca_bundle_falls_through_to_default():
    # A non-existent path must not crash — it falls back to certifi/system default.
    ctx = sidecar.build_ssl_context("https://server", {"ca_bundle": "/no/such/ca.pem"})
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_github_context_stays_verifying_under_insecure(monkeypatch):
    # The insecure opt-in targets the user's own server, never GitHub downloads.
    monkeypatch.setenv("RUNWAY_INSECURE", "1")
    ctx = _github_ssl_context("https://api.github.com/repos/x")
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_reaped_certifi_bundle_falls_through_to_default(monkeypatch):
    # Regression for the dead-sidecar incident: a PyInstaller onefile's /tmp
    # extraction dir (holding certifi's cacert.pem) got swept out from under a
    # long-running daemon, so certifi.where() pointed at a path that no longer
    # existed and every push died with FileNotFoundError ([Errno 2]). The
    # existence guard must fall through to the OS trust store instead of
    # raising.
    monkeypatch.setattr(tls, "_certifi_cafile", lambda: "/tmp/_MEIreaped/cacert.pem")
    ctx = tls.build_context("https://server")
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


# --- config threading (B1): every real network call site must honour the
# sidecar's own ca_bundle / tls_insecure config, not just sidecar.build_ssl_context
# in isolation. Each call site used to call build_context(url) / build_ssl_context(url)
# with NO config, silently ignoring an operator's insecure/ca_bundle opt-in.


class _FakeResp:
    def __init__(self, body=b"{}", code=200):
        self._body = body
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.code

    def read(self):
        return self._body


def test_health_check_honours_tls_insecure_config(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _FakeResp()

    monkeypatch.setattr(sidecar.request, "urlopen", fake_urlopen)
    assert sidecar.health_check("https://server", config={"tls_insecure": True}) is True
    assert captured["context"].verify_mode == ssl.CERT_NONE


def test_http_post_signed_honours_tls_insecure_config(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _FakeResp(body=b'{"status": "ok"}')

    monkeypatch.setattr(sidecar.request, "urlopen", fake_urlopen)
    success, _result, _code = sidecar.http_post_signed(
        "https://server/x", {"a": 1}, "key", config={"tls_insecure": True}
    )
    assert success
    assert captured["context"].verify_mode == ssl.CERT_NONE


def test_http_post_signed_with_retry_threads_config(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _FakeResp(body=b'{"status": "ok"}')

    monkeypatch.setattr(sidecar.request, "urlopen", fake_urlopen)
    success, _result, _code = sidecar.http_post_signed_with_retry(
        "https://server/x", {"a": 1}, "key", config={"tls_insecure": True}
    )
    assert success
    assert captured["context"].verify_mode == ssl.CERT_NONE


def test_fetch_config_payload_honours_tls_insecure_config(monkeypatch):
    from scripts.sidecar_pkg import credentials

    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _FakeResp(body=b'{"config": {}}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = credentials._fetch_config_payload("https://server", config={"tls_insecure": True})
    assert payload == {"config": {}}
    assert captured["context"].verify_mode == ssl.CERT_NONE


def test_pairing_redeem_honours_tls_insecure_config(monkeypatch):
    from scripts.sidecar_pkg import pairing

    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _FakeResp(body=b'{"api_url": "https://server", "api_key": "k"}')

    monkeypatch.setattr(pairing.request, "urlopen", fake_urlopen)
    target = pairing.PairTarget(server="https://server", code="ABCD1234")
    creds = pairing.redeem(target, config={"tls_insecure": True})
    assert creds == {"api_url": "https://server", "api_key": "k"}
    assert captured["context"].verify_mode == ssl.CERT_NONE


def test_manifest_post_honours_tls_insecure_config(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _FakeResp(body=b"{}")

    monkeypatch.setattr(sidecar.urllib.request, "urlopen", fake_urlopen)
    sidecar._post_credential_manifest(
        api_url="https://server",
        api_key="key",  # pragma: allowlist secret
        sidecar_id="host",
        entries=[],
        config={"tls_insecure": True},
    )
    assert captured["context"].verify_mode == ssl.CERT_NONE


def test_queue_flush_config_reaches_the_post(monkeypatch, tmp_path):
    """queue_flush's own config param must reach http_post_signed_with_retry,
    not just get accepted and dropped."""
    captured = {}

    def fake_post(url, data, api_key, config=None):
        captured["config"] = config
        return True, {"status": "ok"}, 200

    monkeypatch.setattr(sidecar, "http_post_signed", fake_post)
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path)

    queue_dir = tmp_path
    queue_dir.mkdir(exist_ok=True)
    (queue_dir / "1.jsonl").write_text('{"payload": {"a": 1}}\n', encoding="utf-8")

    sentinel_config = {"tls_insecure": True}
    sidecar.queue_flush("https://server", "key", config=sentinel_config)
    assert captured["config"] is sentinel_config
