import os
import threading
import time
from pathlib import Path

from sidecar_app.config import watch_config


def test_invalid_config_does_not_kill_watcher(monkeypatch, tmp_path):
    from sidecar_app import config

    path = tmp_path / "config.json"
    path.write_text("initial")
    rejected = threading.Event()
    corrected = threading.Event()
    stop = threading.Event()

    def load(filename):
        if Path(filename).read_text() == "invalid-http":
            rejected.set()
            raise SystemExit(1)
        return {"api_url": "https://corrected.test"}

    monkeypatch.setattr(config, "load_config", load)
    watcher = watch_config(path, lambda _: corrected.set(), stop, poll_interval=0.01)
    try:
        time.sleep(0.05)
        path.write_text("invalid-http")
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 1_000_000))
        assert rejected.wait(2)
        assert watcher.is_alive()
        path.write_text("corrected-https")
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 2_000_000))
        assert corrected.wait(2)
    finally:
        stop.set()
        watcher.join(timeout=2)
