"""Exercise the frozen bootstrap with TUF's real TLS transport, no OS trust edits."""

import os
import runpy
import socket
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import certifi
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from tuf.api.exceptions import DownloadError
from tuf.ngclient.urllib3_fetcher import Urllib3Fetcher
from urllib3.exceptions import MaxRetryError, SSLError

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "sidecar_app/rthook_macos_ca.py"
PUBLIC_CA_BUNDLE = certifi.where()


def bootstrap(monkeypatch, *, platform="darwin", frozen=True):
    # Restore the real platform before urllib's proxy discovery/network calls.
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", platform)
        patch.setattr(sys, "frozen", frozen, raising=False)
        runpy.run_path(str(HOOK))


@pytest.fixture
def tls_endpoint(tmp_path, monkeypatch):
    """Private test root and localhost leaf: the machine never trusts this root."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Runway synthetic CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(ca_ski, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False
        )
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    ca_path, leaf_path, key_path = (tmp_path / n for n in ("ca.pem", "leaf.pem", "key.pem"))
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    leaf_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"synthetic TUF transport fixture")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(leaf_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Track initial absence too: the runtime hook writes directly to os.environ.
    monkeypatch.setenv("SSL_CERT_FILE", os.environ.get("SSL_CERT_FILE", ""))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    empty = tmp_path / "empty-ca-directory"
    empty.mkdir()
    monkeypatch.setenv("SSL_CERT_DIR", str(empty))
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(certifi, "where", lambda: str(ca_path))
    try:
        yield ca_path, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fetch(port, host="localhost"):
    # Keep TUF's normal timeout: Windows localhost/TLS setup can exhaust two seconds.
    return Urllib3Fetcher().download_bytes(f"https://{host}:{port}/root.json", 4096)


def tls_rejection(port, host="localhost"):
    with pytest.raises(DownloadError) as failure:
        fetch(port, host)
    assert isinstance(failure.value.__cause__, MaxRetryError)
    assert isinstance(failure.value.__cause__.reason, SSLError)


def test_tuf_default_fetcher_needs_the_bundled_ca_bootstrap(tls_endpoint, monkeypatch):
    ca_path, port = tls_endpoint
    # certifi.where() already resolves the fixture CA; urllib3 does not use it.
    tls_rejection(port)
    bootstrap(monkeypatch)
    assert os.environ["SSL_CERT_FILE"] == str(ca_path)
    assert fetch(port) == b"synthetic TUF transport fixture"


def test_bootstrap_does_not_disable_tuf_hostname_verification(tls_endpoint, monkeypatch):
    _, port = tls_endpoint
    monkeypatch.setenv("RUNWAY_INSECURE", "1")
    bootstrap(monkeypatch)
    assert fetch(port)
    tls_rejection(port, "127.0.0.1")


def test_fixture_chain_passes_strict_openssl_validation(tls_endpoint):
    ca_path, port = tls_endpoint
    context = ssl.create_default_context(cafile=str(ca_path))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        with context.wrap_socket(connection, server_hostname="localhost") as tls:
            assert tls.getpeercert()


@pytest.mark.parametrize("explicit", [False, True])
def test_untrusted_chain_is_rejected_even_with_insecure_ingest_settings(
    tls_endpoint, monkeypatch, explicit
):
    ca_path, port = tls_endpoint
    if explicit:
        monkeypatch.setenv("SSL_CERT_FILE", PUBLIC_CA_BUNDLE)
    else:
        monkeypatch.setattr(certifi, "where", lambda: PUBLIC_CA_BUNDLE)
    monkeypatch.setenv("RUNWAY_INSECURE", "1")
    monkeypatch.setenv("RUNWAY_CA_BUNDLE", str(ca_path))
    bootstrap(monkeypatch)
    tls_rejection(port)
    assert os.environ["SSL_CERT_FILE"] == PUBLIC_CA_BUNDLE


@pytest.mark.parametrize("value", ["/explicit/ca.pem", "", "/missing/ca.pem"])
def test_operator_override_is_preserved_even_if_invalid(monkeypatch, value):
    monkeypatch.setenv("SSL_CERT_FILE", value)
    monkeypatch.setattr(certifi, "where", lambda: pytest.fail("explicit override must win"))
    bootstrap(monkeypatch)
    assert os.environ["SSL_CERT_FILE"] == value


@pytest.mark.parametrize("platform,frozen", [("darwin", False), ("win32", True), ("linux", True)])
def test_other_runtimes_are_unchanged(monkeypatch, platform, frozen):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    bootstrap(monkeypatch, platform=platform, frozen=frozen)
    assert "SSL_CERT_FILE" not in os.environ


def test_macos_spec_wires_bootstrap_before_application_entrypoint():
    spec = (ROOT / "sidecar_app/spec/macos.spec").read_text()
    assert 'runtime_hooks=[os.path.join(_ROOT, "sidecar_app", "rthook_macos_ca.py")]' in spec
    assert '"certifi",' in spec  # Its PyInstaller hook collects the CA data file.
