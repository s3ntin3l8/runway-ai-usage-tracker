"""Isolated real HTTP services for the browser security matrix. No collectors."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

if len(sys.argv) > 1:
    import uvicorn

    from app.core.db import init_db
    from app.main import app

    init_db()
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), lifespan="off", proxy_headers=False)
    raise SystemExit

from sidecar_app.settings_server import SettingsServer

children = []
sidecar_state = {"saves": 0, "api_url": "http://localhost:8765", "api_key": "synthetic-fleet-key"}


def save(config):
    sidecar_state.update(config)
    sidecar_state["saves"] += 1


sidecar = SettingsServer(
    get_config=lambda: dict(sidecar_state),
    get_status=lambda: {"version": "test", "sidecar_id": "fixture"},
    save_config=save,
    open_dashboard=lambda: None,
    open_logs=lambda: None,
    open_config=lambda: None,
    port=18767,
)
assert sidecar.start() == 18767


def cleanup(*args):
    for child in children:
        child.terminate()
    for child in children:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    raise SystemExit


signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)
with tempfile.TemporaryDirectory(prefix="runway-browser-") as temp:
    from cryptography.fernet import Fernet

    for port, host in [(18765, "127.0.0.1"), (18766, "0.0.0.0")]:
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "VIRTUAL_ENV", "LANG"}}
        env.update(
            RUNWAY_CONFIG_DIR=str(Path(temp) / str(port)),
            APP_HOST=host,
            ADMIN_API_KEY="synthetic-browser-admin",
            INGEST_API_KEY="synthetic-browser-ingest",
            TLS_TERMINATED="1",
            DB_ENCRYPTION_KEY=Fernet.generate_key().decode(),
            CORS_ORIGINS=f"http://127.0.0.1:{port}",
            PYTHONPATH=str(Path.cwd()),
        )
        children.append(subprocess.Popen([sys.executable, __file__, str(port)], env=env))
        for attempt in range(100):
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/v1/system/health", timeout=1):
                    break
            except OSError:
                if children[-1].poll() is not None:
                    cleanup()
                time.sleep(0.1)
        else:
            cleanup()

    class Attacker(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/json" if self.path == "/state" else "text/html"
            )
            self.end_headers()
            self.wfile.write(
                json.dumps({"saves": sidecar_state["saves"]}).encode()
                if self.path == "/state"
                else b"<!doctype html><title>Security fixture</title>"
            )

        def log_message(self, *args):
            pass

    try:
        ThreadingHTTPServer(("127.0.0.1", 18768), Attacker).serve_forever()
    finally:
        cleanup()
