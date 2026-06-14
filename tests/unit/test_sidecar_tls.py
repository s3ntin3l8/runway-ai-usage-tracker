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

from scripts.sidecar_pkg.self_update import _github_ssl_context
from scripts.sidecar_pkg.tls import build_context

# Import sidecar as a module (it lives in scripts/, not a package)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import sidecar  # noqa: E402


def test_http_url_returns_no_context():
    assert build_context("http://server:8765") is None
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
