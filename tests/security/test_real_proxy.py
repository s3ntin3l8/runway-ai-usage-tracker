"""Real nginx + Uvicorn trust validation; explicitly enabled in security CI."""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

pytestmark = pytest.mark.skipif(
    os.environ.get("RUNWAY_PROXY_TEST") != "1",
    reason="Enable RUNWAY_PROXY_TEST=1 for isolated Docker proxy validation",
)
NGINX = "nginx@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94"


def test_proxy_assertions_and_sidecar_bypass(tmp_path):
    class Identity(BaseHTTPRequestHandler):
        def do_GET(self):
            allowed = self.headers.get("Cookie") == "fixture-session=valid"
            self.send_response(200 if allowed else 401)
            if allowed:
                self.send_header("X-Forwarded-User", "fixture-admin")
                self.send_header("X-Forwarded-Groups", "runway-admins")
            self.end_headers()

        def log_message(self, *args):
            pass

    identity = ThreadingHTTPServer(("127.0.0.1", 18871), Identity)
    worker = threading.Thread(target=identity.serve_forever, daemon=True)
    worker.start()
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG"}}
    env.update(
        PYTHONPATH=str(Path.cwd()),
        RUNWAY_CONFIG_DIR=str(tmp_path / "data"),
        APP_HOST="0.0.0.0",
        ADMIN_API_KEY="synthetic-proxy-admin",
        INGEST_API_KEY="synthetic-proxy-ingest",
        DB_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        TLS_TERMINATED="1",
        CORS_ORIGINS="http://127.0.0.1:18870",
        TRUSTED_PROXY_IPS="127.0.0.1",
        FORWARD_AUTH_ALLOWED_GROUPS="runway-admins",
    )
    backend = subprocess.Popen([sys.executable, "tests/browser/server.py", "18872"], env=env)
    container = None
    try:
        config = tmp_path / "nginx.conf"
        config.write_text("""events {}
http {
 server {
  listen 127.0.0.1:18870;
  location = /_identity {
   internal;
   proxy_pass http://127.0.0.1:18871;
   proxy_pass_request_body off;
   proxy_set_header Content-Length "";
  }
  location ~ ^/api/v1/fleet/(ingest|config|credentials/manifest)$ {
   proxy_pass http://127.0.0.1:18872;
   proxy_set_header Host $http_host;
   proxy_set_header X-Forwarded-User "";
   proxy_set_header Remote-User "";
   proxy_set_header X-Forwarded-Groups "";
   proxy_set_header X-Forwarded-For $remote_addr;
  }
  location / {
   auth_request /_identity;
   auth_request_set $user $upstream_http_x_forwarded_user;
   auth_request_set $groups $upstream_http_x_forwarded_groups;
   proxy_set_header X-Forwarded-User $user;
   proxy_set_header X-Forwarded-Groups $groups;
   proxy_set_header Remote-User "";
   proxy_set_header X-Forwarded-For $remote_addr;
   proxy_set_header Host $http_host;
   proxy_pass http://127.0.0.1:18872;
  }
 }
}
""")
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--network",
                "host",
                "-v",
                f"{config}:/etc/nginx/nginx.conf:ro",
                NGINX,
            ],
            text=True,
        ).strip()
        with httpx.Client(
            transport=httpx.HTTPTransport(local_address="127.0.0.2"), trust_env=False, timeout=5
        ) as client:
            for _ in range(100):
                try:
                    if (
                        client.get(
                            "http://127.0.0.1:18870/api/v1/system/settings",
                            headers={"Cookie": "fixture-session=valid"},
                        ).status_code
                        == 200
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Proxy did not become ready")
            private = "/api/v1/system/dashboard-layout"
            forged = {
                "X-Forwarded-User": "fixture-admin",
                "X-Forwarded-Groups": "runway-admins",
                "X-Forwarded-For": "127.0.0.1",
                "Remote-User": "fixture-admin",
            }
            assert client.get("http://127.0.0.1:18872" + private, headers=forged).status_code == 403
            assert client.get("http://127.0.0.1:18870" + private, headers=forged).status_code == 401
            accepted = client.get(
                "http://127.0.0.1:18870" + private,
                headers={**forged, "Cookie": "fixture-session=valid"},
            )
            assert accepted.status_code == 200
            context = client.get(
                "http://127.0.0.1:18870/api/v1/system/settings",
                headers={"Cookie": "fixture-session=valid"},
            ).json()
            assert context["is_authenticated"] is True
            assert context["user_context"] == "fixture-admin"
            assert (
                client.post("http://127.0.0.1:18870/api/v1/fleet/ingest", json={}).status_code
                == 401
            )
            body = json.dumps(
                {"provider": "proxy-fixture", "sidecar_id": "proxy-fixture", "metrics": []}
            ).encode()
            ts = str(time.time())
            signed = {
                "X-Timestamp": ts,
                "X-Signature": hmac.new(
                    b"synthetic-proxy-ingest", ts.encode() + body, hashlib.sha256
                ).hexdigest(),
            }
            assert (
                client.post(
                    "http://127.0.0.1:18870/api/v1/fleet/ingest", content=body, headers=signed
                ).status_code
                == 200
            )
    finally:
        if container:
            subprocess.run(["docker", "stop", container], check=True, capture_output=True)
        backend.terminate()
        backend.wait(timeout=10)
        identity.shutdown()
        identity.server_close()
        worker.join()
