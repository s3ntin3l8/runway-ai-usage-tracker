"""Real loopback helper transports; these are not physical browser acceptance."""

import errno
import http.client
import json
import socket
import ssl
import struct
import threading
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from scripts.security_browser_helper import (
    EventLog,
    Fixture,
    Helper,
    http_server,
    https_server,
    servers,
)
from scripts.security_browser_probe import PAGE
from tests.unit.test_physical_security_tools import question


@pytest.fixture
def helper(tmp_path):
    return Helper(
        [
            Fixture.parse("sidecar,sidecar.example,http://127.0.0.1:18653"),
            Fixture.parse("dashboard,dashboard.example,http://127.0.0.1:18655"),
        ],
        "127.0.0.1",
        ["127.0.0.1/32"],
        EventLog(tmp_path / "events.jsonl"),
        "tls.example",
    )


@pytest.mark.parametrize(
    "value",
    [
        "sidecar,sidecar.example,http://192.0.2.1:18653",
        "sidecar,sidecar.example,https://127.0.0.1:18653",
        "sidecar,sidecar.example,http://owner:password@127.0.0.1:18653",  # pragma: allowlist secret
        "sidecar,sidecar.example,http://127.0.0.1:18653/?credential=value",
        "sidecar,sidecar.example,http://127.0.0.1:18653/save",
        "sidecar,<script>.example,http://127.0.0.1:18653",
        "sidecar,sidecar.example,http://127.0.0.1",
        "openai,sidecar.example,http://127.0.0.1:18653",
    ],
)
def test_fixture_refuses_remote_targets_credentials_and_unsafe_names(value):
    with pytest.raises(ValueError):
        Fixture.parse(value)


def test_bind_and_client_allowlist_are_required(helper):
    with pytest.raises(ValueError):
        Helper(list(helper.fixtures.values()), "0.0.0.0", ["127.0.0.1/32"], helper.log)
    with pytest.raises(ValueError):
        Helper(list(helper.fixtures.values()), "127.0.0.1", [], helper.log)
    assert helper.allowed("127.0.0.1")
    assert not helper.allowed("127.0.0.2")


def test_independent_arm_logs_real_answer_and_never_recurses(helper):
    helper = Helper(
        list(helper.fixtures.values()), "192.0.2.1", ["127.0.0.1/32"], helper.log, "tls.example"
    )
    initial = helper.answer(question("sidecar.example"), "127.0.0.1", "udp")
    helper.arm(helper.names["sidecar"], "127.0.0.1")
    rebound = helper.answer(question("sidecar.example"), "127.0.0.1", "udp")
    assert initial[-4:] == socket.inet_aton("192.0.2.1")
    assert rebound[-4:] == socket.inet_aton("127.0.0.1")
    assert helper.answer(question("sidecar.example"), "127.0.0.1", "tcp")[-4:] == socket.inet_aton(
        "127.0.0.1"
    )
    assert helper.answer(question("dashboard.example"), "127.0.0.1", "udp")[
        -4:
    ] == socket.inet_aton("192.0.2.1")
    assert helper.answer(question("tls.example"), "127.0.0.1", "udp")[-4:] == socket.inet_aton(
        "192.0.2.1"
    )
    unknown = helper.answer(question("unrelated-sensitive.example"), "127.0.0.1", "udp")
    assert struct.unpack_from("!H", unknown, 2)[0] & 15 == 3
    assert struct.unpack_from("!H", unknown, 6)[0] == 0
    ipv6 = helper.answer(question("sidecar.example", kind=28), "127.0.0.1", "udp")
    assert struct.unpack_from("!H", ipv6, 6)[0] == 0
    rows = [json.loads(line) for line in helper.log.path.read_text().splitlines()]
    assert all(row["at"].endswith("+00:00") for row in rows)
    assert any(row["event"] == "arm" and row["fixture"] == "sidecar" for row in rows)
    assert all(row["ttl"] == 0 for row in rows if row.get("answer") is not None)
    assert "unrelated-sensitive.example" not in helper.log.path.read_text()


def test_udp_and_fragmented_tcp_dns_use_same_port(helper):
    udp, tcp = servers(helper, 0)
    with ExitStack() as stack:
        stack.enter_context(udp)
        stack.enter_context(tcp)
        threads = [
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.01})
            for s in (udp, tcp)
        ]
        for thread in threads:
            thread.start()
        try:
            query = question("dashboard.example")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(2)
                client.sendto(query, udp.server_address)
                answer, _ = client.recvfrom(4096)
                assert answer[-4:] == socket.inet_aton("127.0.0.1")
            assert udp.server_address == tcp.server_address
            with socket.create_connection(tcp.server_address, timeout=2) as client:
                frame = struct.pack("!H", len(query)) + query
                client.sendall(frame[:1])
                time.sleep(0.02)
                client.sendall(frame[1:])
                received = bytearray()
                while len(received) < len(query) + 18:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    received.extend(chunk)
                assert received[2:4] == query[:2]
                assert received[-4:] == socket.inet_aton("127.0.0.1")
            transports = {
                json.loads(line)["transport"] for line in helper.log.path.read_text().splitlines()
            }
            assert transports == {"udp", "tcp"}
        finally:
            udp.shutdown()
            tcp.shutdown()
            for thread in threads:
                thread.join()


def test_http_arm_requires_real_host_and_origin_and_records_no_bodies(helper):
    fixture = helper.names["sidecar"]
    with http_server(helper, 0, fixture) as server:
        fixture.target = f"http://127.0.0.1:{server.server_port}"
        origin = f"http://{fixture.hostname}:{fixture.port}"
        host = f"{fixture.hostname}:{fixture.port}"
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:

            def request(method, path, headers, body=None):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                client.request(method, path, body=body, headers=headers)
                response = client.getresponse()
                result = (response.status, response.read(), response.getheader("X-Runway-Probe"))
                client.close()
                return result

            status, body, marker = request("GET", "/", {"Host": host})
            assert status == 200 and marker == "helper"
            assert fixture.target.encode() in body
            assert b"synthetic-fleet-key" not in body
            assert request("POST", "/arm", {"Host": host})[0] == 403
            assert (
                request("POST", "/arm", {"Host": host, "Origin": "https://wrong.example"})[0] == 403
            )
            assert request("POST", "/arm", {"Host": "wrong.example", "Origin": origin})[0] == 403
            assert not fixture.armed
            assert (
                request("POST", "/arm", {"Host": host, "Origin": origin}, b"do-not-log-this-body")[
                    0
                ]
                == 204
            )
            assert fixture.armed and not helper.names["dashboard"].armed
            assert "do-not-log-this-body" not in helper.log.path.read_text()
            assert request("GET", "/", {"Host": "wrong.example"})[0] == 403
            assert request("PUT", "/api/v1/system/dashboard-layout", {"Host": host})[0] == 405
            assert request("PUT", "/", {"Host": "wrong.example"})[0] == 403
            # Admitting the helper's own /32 does not admit another LAN peer.
            with socket.socket() as forbidden:
                forbidden.bind(("127.0.0.2", 0))
                forbidden.settimeout(1)
                forbidden.connect(server.server_address)
                assert forbidden.recv(1) == b""
        finally:
            server.shutdown()
            thread.join()


def test_page_marker_control_is_not_a_false_positive_on_its_own_html():
    assert "synthetic-fleet-key" not in PAGE
    assert "helperResponse" in PAGE


def test_actual_tls_routes_serve_target_pages_with_certificate_verification(helper, tmp_path):
    # Test-only CA, trusted by this Python context only; no host/browser trust changes.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tls.example")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("tls.example")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    keyfile.chmod(0o600)
    with https_server(helper, 0, certfile, keyfile) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        stalled = socket.create_connection(server.server_address, timeout=2)
        forbidden = socket.socket()
        forbidden.bind(("127.0.0.2", 0))
        forbidden.settimeout(1)
        forbidden.connect(server.server_address)
        assert forbidden.recv(1) == b""
        forbidden.close()
        context = ssl.create_default_context(cafile=str(certfile))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            for fixture in helper.names.values():
                with socket.create_connection(server.server_address, timeout=2) as plain:
                    with context.wrap_socket(plain, server_hostname="tls.example") as client:
                        request = f"GET /{fixture.name} HTTP/1.1\r\nHost: tls.example:{server.server_port}\r\nConnection: close\r\n\r\n"
                        client.sendall(request.encode())
                        response = bytearray()
                        while chunk := client.recv(4096):
                            response.extend(chunk)
                        assert b"200 OK" in response
                        assert fixture.target.encode() in response
                        assert b"synthetic-fleet-key" not in response
            rows = [json.loads(line) for line in helper.log.path.read_text().splitlines()]
            assert {row["path"] for row in rows} == {"/sidecar", "/dashboard"}
        finally:
            started = time.monotonic()
            server.shutdown()
            thread.join(timeout=1)
            assert not thread.is_alive()
            assert time.monotonic() - started < 1
            stalled.close()


@pytest.mark.parametrize("bind_errno", [errno.EACCES, errno.EADDRINUSE])
def test_ephemeral_dns_reselects_and_closes_tcp_when_udp_port_is_unavailable(
    helper, monkeypatch, bind_errno
):
    import socketserver

    original_bind = socketserver.ThreadingUDPServer.server_bind
    original_close = socketserver.ThreadingTCPServer.server_close
    attempts = []
    closed = []

    def bind_udp(server):
        attempts.append(server.server_address)
        if len(attempts) == 1:
            raise OSError(bind_errno, "synthetic unavailable UDP port")
        return original_bind(server)

    def close_tcp(server):
        closed.append(server.server_address)
        return original_close(server)

    monkeypatch.setattr(socketserver.ThreadingUDPServer, "server_bind", bind_udp)
    monkeypatch.setattr(socketserver.ThreadingTCPServer, "server_close", close_tcp)
    udp, tcp = servers(helper, 0)
    try:
        assert len(attempts) == 2
        assert closed == [attempts[0]]
        assert udp.server_address == tcp.server_address
    finally:
        udp.server_close()
        tcp.server_close()


@pytest.mark.parametrize("port, attempts", [(0, 5), (19443, 1)])
def test_dns_allocation_failure_is_bounded_and_closes_tcp(helper, monkeypatch, port, attempts):
    import socketserver

    closed = []
    binds = []
    original_tcp_bind = socketserver.ThreadingTCPServer.server_bind
    original_close = socketserver.ThreadingTCPServer.server_close

    def bind_tcp(server):
        # Use a real owned socket without depending on the configured fixture's
        # port being available on the test host. Keep servers()' retry policy real.
        server.server_address = (helper.bind, 0)
        original_tcp_bind(server)

    def reject_udp(server):
        binds.append(server.server_address)
        raise PermissionError(errno.EACCES, "synthetic UDP exclusion")

    def close_tcp(server):
        closed.append(server.server_address)
        original_close(server)

    monkeypatch.setattr(socketserver.ThreadingTCPServer, "server_bind", bind_tcp)
    monkeypatch.setattr(socketserver.ThreadingUDPServer, "server_bind", reject_udp)
    monkeypatch.setattr(socketserver.ThreadingTCPServer, "server_close", close_tcp)
    with pytest.raises(PermissionError):
        servers(helper, port)
    assert len(binds) == attempts
    assert closed == binds
