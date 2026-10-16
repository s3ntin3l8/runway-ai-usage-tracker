"""A throwaway, isolated Runway with synthetic demo data.

    make demo                      # serve it until Ctrl-C, then delete everything
    make screenshots               # README + manifest screenshots (scripts/demo/capture.py)
    make screenshots-landing OUT=…  # landing-page stills + loop

All state lives in one mkdtemp directory: the database (RUNWAY_CONFIG_DIR), HOME
and the XDG dirs. The server runs from a scratch working directory without a
`.env`, on a loopback port that must be free, with an environment built from
scratch (scripts/demo/env.py refuses anything that points elsewhere). It never
touches Docker, so a compose deployment on the same host is out of reach.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from scripts.demo.env import assert_demo_env, assert_ports_free, demo_env

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "bin" / "python"
DEMO_PORT = int(os.environ.get("DEMO_PORT", "3965"))


def _wait_for_http(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as res:  # noqa: S310 — loopback URL we built
                if res.status < 500:
                    return
        except (urllib.error.URLError, ConnectionError) as e:
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


def ensure_spa_built() -> None:
    """The server serves webapp/dist; build it once if it's missing."""
    if (ROOT / "webapp" / "dist" / "index.html").exists():
        return
    print("[demo] building the SPA (webapp/dist)...")
    subprocess.run(["npm", "--prefix", str(ROOT / "webapp"), "run", "build"], check=True)


@dataclass
class Demo:
    demo_dir: Path
    url: str
    server: subprocess.Popen[bytes]
    keep: bool

    def stop(self) -> None:
        if self.server.poll() is None:
            # Own session (start_new_session) so the whole group goes.
            os.killpg(self.server.pid, signal.SIGTERM)
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.server.pid, signal.SIGKILL)
                self.server.wait()
        if self.keep:
            print(f"[demo] kept {self.demo_dir}")
            return
        shutil.rmtree(self.demo_dir, ignore_errors=True)


def start_demo(*, keep: bool = False, quiet: bool = False) -> Demo:
    assert_ports_free([DEMO_PORT])
    ensure_spa_built()

    demo_dir = Path(tempfile.mkdtemp(prefix="runway-demo-"))
    run_dir = demo_dir / "run"  # server cwd: no .env here
    for sub in ("home", "tmp", "run", "config"):
        (demo_dir / sub).mkdir(parents=True, exist_ok=True)

    env = demo_env(
        demo_dir,
        {
            "PYTHONPATH": str(ROOT),
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(DEMO_PORT),
            # The poller still runs, but its collectors only find the scratch
            # HOME's empty credential stores, so the seeded cards stay as seeded.
            "LOG_LEVEL": "WARNING",
        },
    )
    server: subprocess.Popen[bytes] | None = None
    try:
        assert_demo_env(env, demo_dir, cwd=run_dir)
        print(f"[demo] scratch dir {demo_dir}")
        print("[demo] seeding demo data...")
        subprocess.run(
            [str(PYTHON), "-m", "scripts.demo.seed_demo"], cwd=run_dir, env=env, check=True
        )
        print("[demo] starting the server...")
        server = subprocess.Popen(
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(DEMO_PORT),
                "--no-proxy-headers",
            ],
            cwd=run_dir,
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL if quiet else None,
        )
        url = f"http://127.0.0.1:{DEMO_PORT}"
        _wait_for_http(f"{url}/")
        return Demo(demo_dir=demo_dir, url=url, server=server, keep=keep)
    except BaseException:
        if server is not None:
            Demo(demo_dir=demo_dir, url="", server=server, keep=keep).stop()
        elif not keep:
            shutil.rmtree(demo_dir, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the scratch dir on exit")
    args = parser.parse_args()
    demo = start_demo(keep=args.keep)
    print(f"\n[demo] Runway demo is up: {demo.url}")
    print("[demo] Loopback only, no login. Ctrl-C stops it and deletes all demo data.")
    try:
        demo.server.wait()
    except KeyboardInterrupt:
        pass  # Ctrl-C is the documented way to stop; teardown runs below.
    finally:
        demo.stop()
    sys.exit(0)


if __name__ == "__main__":
    main()
