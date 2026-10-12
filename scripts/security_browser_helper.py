"""Isolated multi-target HTTP/TLS pages and authoritative rebinding DNS.

No credentials, DNS forwarding, production proxy integration, or browser controls.
"""

import argparse
import hashlib
import ipaddress
import json
import re
import signal
import socketserver
import ssl
import struct
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from scripts.security_browser_probe import dns_answer, dns_question, render_page


@dataclass
class Fixture:
    name: str
    hostname: str
    target: str
    armed: bool = False

    @property
    def port(self) -> int:
        return urlsplit(self.target).port or 80

    @classmethod
    def parse(cls, value: str) -> "Fixture":
        name, hostname, target = value.split(",", 2)
        parsed = urlsplit(target)
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name)
            or not valid_hostname(hostname)
            or parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise ValueError("Fixture must be name,controlled-FQDN,http://loopback:port")
        return cls(name, hostname.lower(), target.rstrip("/"))


def valid_hostname(hostname: str) -> bool:
    return (
        len(hostname) <= 253
        and "." in hostname
        and all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
            for part in hostname.split(".")
        )
    )


class EventLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        row = {"at": datetime.now(UTC).isoformat(), "event": event, **fields}
        line = json.dumps(row, sort_keys=True)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as output:
                output.write(line + "\n")
            self.path.chmod(0o600)
            print(line, flush=True)


class Helper:
    def __init__(
        self,
        fixtures: list[Fixture],
        bind: str,
        allowed_clients: list[str],
        log: EventLog,
        tls_hostname: str | None = None,
    ) -> None:
        address = ipaddress.IPv4Address(bind)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Bind must be a specific unicast helper IPv4")
        if not fixtures or not allowed_clients:
            raise ValueError("At least one fixture and an explicit client allow-list are required")
        if (
            len({f.hostname for f in fixtures}) != len(fixtures)
            or len({f.name for f in fixtures}) != len(fixtures)
            or len({f.port for f in fixtures}) != len(fixtures)
        ):
            raise ValueError("Fixture names, hostnames and HTTP ports must be distinct")
        if tls_hostname and (
            not valid_hostname(tls_hostname) or tls_hostname in {f.hostname for f in fixtures}
        ):
            raise ValueError("TLS hostname must be a distinct controlled FQDN")
        self.fixtures = {f.hostname: f for f in fixtures}
        self.names = {f.name: f for f in fixtures}
        self.bind = bind
        self.clients = [ipaddress.IPv4Network(c, strict=False) for c in allowed_clients]
        self.log = log
        self.tls_hostname = tls_hostname
        self.lock = threading.Lock()

    def allowed(self, address: str) -> bool:
        return any(ipaddress.IPv4Address(address) in network for network in self.clients)

    def answer(self, query: bytes, client: str, transport: str) -> bytes:
        hostname, qtype, qclass, _ = dns_question(query)
        with self.lock:
            fixture = self.fixtures.get(hostname)
            controlled = fixture is not None or hostname == self.tls_hostname
            address = "127.0.0.1" if fixture and fixture.armed else self.bind
            answer = dns_answer(query, hostname if controlled else "", address)
            self.log.write(
                "dns",
                client=client,
                transport=transport,
                hostname=hostname if controlled else "[unconfigured name omitted]",
                qtype=qtype,
                qclass=qclass,
                rcode=struct.unpack_from("!H", answer, 2)[0] & 15,
                answer=address if controlled and qtype == 1 and qclass == 1 else None,
                ttl=0 if controlled and qtype == 1 and qclass == 1 else None,
                armed=bool(fixture and fixture.armed),
            )
        return answer

    def arm(self, fixture: Fixture, client: str) -> None:
        with self.lock:
            fixture.armed = True
            self.log.write("arm", fixture=fixture.name, hostname=fixture.hostname, client=client)

    def handler(self, fixture: Fixture | None = None) -> type[BaseHTTPRequestHandler]:
        helper = self

        class HTTP(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                pass

            def reply(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Runway-Probe", "helper")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                helper.log.write(
                    "helper-http",
                    client=self.client_address[0],
                    method=self.command,
                    path=self.path.split("?", 1)[0],
                    status=status,
                    fixture=fixture.name if fixture else "tls-direct",
                    port=cast(ThreadingHTTPServer, self.server).server_port,
                )

            def trusted_host(self) -> bool:
                expected = fixture.hostname if fixture else helper.tls_hostname
                return (
                    self.headers.get("Host", "")
                    == f"{expected}:{cast(ThreadingHTTPServer, self.server).server_port}"
                )

            def do_GET(self) -> None:
                if not self.trusted_host():
                    self.reply(403)
                    return
                selected = fixture if self.path == "/" else None
                if fixture is None:
                    selected = helper.names.get(self.path.removeprefix("/"))
                if selected is None:
                    self.reply(404)
                    return
                kind = selected.name if selected.name in {"sidecar", "dashboard"} else "both"
                self.reply(200, render_page(selected.target, kind))

            def do_POST(self) -> None:
                expected_origin = f"http://{fixture.hostname}:{fixture.port}" if fixture else ""
                if not self.trusted_host() or self.headers.get("Origin", "") != expected_origin:
                    self.reply(403)
                    return
                if self.path != "/arm" or fixture is None:
                    self.reply(404)
                    return
                helper.arm(fixture, self.client_address[0])
                self.reply(204)

            def do_PUT(self) -> None:
                self.reply(403 if not self.trusted_host() else 404)

        return HTTP


def servers(
    helper: Helper, dns_port: int
) -> tuple[socketserver.BaseServer, socketserver.BaseServer]:
    class UDP(socketserver.ThreadingUDPServer):
        allow_reuse_address = True
        daemon_threads = True

        def verify_request(self, request: Any, client_address: Any) -> bool:
            return helper.allowed(client_address[0])

    class TCP(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def verify_request(self, request: Any, client_address: Any) -> bool:
            return helper.allowed(client_address[0])

    class UDPHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            query, sock = self.request
            try:
                answer = helper.answer(query, self.client_address[0], "udp")
                sock.sendto(answer, self.client_address)
            except (ValueError, UnicodeError):
                pass

    class TCPHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(3)
            try:
                prefix = bytearray()
                while len(prefix) < 2:
                    chunk = self.request.recv(2 - len(prefix))
                    if not chunk:
                        return
                    prefix.extend(chunk)
                length = struct.unpack("!H", prefix)[0]
                if not 12 <= length <= 4096:
                    return
                query = bytearray()
                while len(query) < length:
                    chunk = self.request.recv(length - len(query))
                    if not chunk:
                        return
                    query.extend(chunk)
                answer = helper.answer(bytes(query), self.client_address[0], "tcp")
                self.request.sendall(struct.pack("!H", len(answer)) + answer)
            except (ValueError, UnicodeError, OSError):
                pass

    udp = UDP((helper.bind, dns_port), UDPHandler)
    try:
        tcp = TCP((helper.bind, udp.server_address[1]), TCPHandler)
    except BaseException:
        udp.server_close()
        raise
    return udp, tcp


def http_server(
    helper: Helper,
    port: int,
    fixture: Fixture | None,
    tls_context: ssl.SSLContext | None = None,
) -> ThreadingHTTPServer:
    class HTTP(ThreadingHTTPServer):
        def verify_request(self, request: Any, client_address: Any) -> bool:
            return helper.allowed(client_address[0])

        def finish_request(self, request: Any, client_address: Any) -> None:
            if tls_context is None:
                super().finish_request(request, client_address)
                return
            # verify_request has already checked the raw peer. Negotiate in
            # this daemon worker with a deadline, never on the accept loop.
            request.settimeout(3)
            try:
                with tls_context.wrap_socket(request, server_side=True) as tls:
                    handler = cast(type[BaseHTTPRequestHandler], self.RequestHandlerClass)
                    handler(tls, client_address, self)
            except (ssl.SSLError, OSError):
                pass

    return HTTP((helper.bind, port), helper.handler(fixture))


def https_server(helper: Helper, port: int, cert: Path, key: Path) -> ThreadingHTTPServer:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return http_server(helper, port, None, context)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", action="append", required=True, help="name,FQDN,http://127.0.0.1:port"
    )
    parser.add_argument("--bind", required=True, help="Dedicated helper IPv4, never wildcard")
    parser.add_argument(
        "--allowed-client", action="append", required=True, help="Explicit IPv4/CIDR"
    )
    parser.add_argument("--dns-port", type=int, default=53)
    parser.add_argument("--tls-hostname")
    parser.add_argument("--https-port", type=int, default=19443)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    if any((args.tls_hostname, args.tls_cert, args.tls_key)) and not all(
        (args.tls_hostname, args.tls_cert, args.tls_key)
    ):
        parser.error("TLS hostname, certificate chain and private key must be supplied together")
    log = EventLog(args.evidence_dir / "helper-events.jsonl")
    helper = Helper(
        [Fixture.parse(f) for f in args.fixture],
        args.bind,
        args.allowed_client,
        log,
        args.tls_hostname,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    running = []
    threads = []
    with ExitStack() as stack:
        for server in servers(helper, args.dns_port):
            running.append(stack.enter_context(server))
        for fixture in helper.fixtures.values():
            running.append(stack.enter_context(http_server(helper, fixture.port, fixture)))
        if args.tls_cert:
            tls = stack.enter_context(
                https_server(helper, args.https_port, args.tls_cert, args.tls_key)
            )
            running.append(tls)
        for server in running:
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
            )
            thread.start()
            threads.append(thread)
        log.write(
            "helper-started",
            bind=helper.bind,
            dns_port=args.dns_port,
            allowed_clients=args.allowed_client,
            fixtures=[
                {"name": f.name, "hostname": f.hostname, "target": f.target, "http_port": f.port}
                for f in helper.fixtures.values()
            ],
            tls_hostname=args.tls_hostname,
            https_port=args.https_port if args.tls_cert else None,
            tls_cert_sha256=hashlib.sha256(args.tls_cert.read_bytes()).hexdigest()
            if args.tls_cert
            else None,
            source_sha256={
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [Path(__file__), Path(__file__).with_name("security_browser_probe.py")]
            },
        )
        try:
            stop.wait()
        finally:
            for server in running:
                server.shutdown()
            for thread in threads:
                thread.join()
            log.write(
                "helper-stopped",
                armed_fixtures=[f.name for f in helper.fixtures.values() if f.armed],
            )


if __name__ == "__main__":
    main()
