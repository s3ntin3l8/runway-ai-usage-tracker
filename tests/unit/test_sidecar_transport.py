import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request

import pytest

from scripts.sidecar_pkg import transport


@pytest.mark.parametrize(
    "url",
    [
        "http://server",
        "http://192.168.1.5",
        "http://2130706433",
        "http://127.1",
        "http://localhost.attacker.test",
        "ftp://localhost",
        "https://host/#fragment",
        "https://user@host",
        "https://host:99999",
        "https://host\n",
    ],
)
def test_invalid_urls_rejected(url):
    with pytest.raises(transport.ServerURLError):
        transport.validate_server_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8765",
        "http://127.0.0.1:1234",
        "http://[::1]:8765",
        "https://server:8443/path",
    ],
)
def test_valid_urls(url):
    assert transport.validate_server_url(url) == url


def test_remote_http_never_opens_connection(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("A rejected URL must not open a connection")

    monkeypatch.setattr(transport.request, "build_opener", unexpected)
    with pytest.raises(transport.ServerURLError):
        transport.urlopen(
            request.Request("http://remote.test", data=b"synthetic-credential"), timeout=1
        )


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_never_forward_headers_or_body(status):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append((self.path, self.headers.get("X-Signature")))
            self.send_response(status if self.path == "/source" else 200)
            self.send_header("Location", "/target")
            self.end_headers()

        do_POST = do_GET  # noqa: N815 — BaseHTTPRequestHandler dispatch name

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = request.Request(
            f"http://127.0.0.1:{server.server_port}/source",
            data=b"synthetic-credential",
            headers={"X-Signature": "synthetic"},
        )
        with pytest.raises(error.HTTPError) as exc:
            transport.urlopen(req, timeout=3)
        assert exc.value.code == status
        assert paths == [("/source", "synthetic")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_signatures_distinct_under_frozen_clock(monkeypatch):
    monkeypatch.setattr(transport.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    monkeypatch.setattr(transport, "_LAST_TIMESTAMP_US", 0)
    timestamps = [transport.signing_timestamp() for _ in range(20)]
    assert len(set(timestamps)) == 20
    assert timestamps == sorted(timestamps)
