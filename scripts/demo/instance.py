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
        # OSError covers URLError, ConnectionError and the bare TimeoutError a
        # slow loopback accept raises during boot.
        except OSError as e:
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


# Everything `npm run build` reads: a change to any of these makes dist stale.
SPA_INPUT_DIRS = ("src", "public")
SPA_INPUT_GLOBS = (
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.*",
    "tsconfig*.json",
)


def _spa_inputs(webapp: Path) -> list[Path]:
    # Directories count too: deleting a file only bumps its parent dir's mtime.
    # webapp itself catches a removed top-level input (index.html, public/, ...).
    paths = [webapp]
    paths += [
        p
        for d in SPA_INPUT_DIRS
        if (webapp / d).is_dir()
        for p in (webapp / d, *(webapp / d).rglob("*"))
    ]
    paths += [p for pattern in SPA_INPUT_GLOBS for p in webapp.glob(pattern) if p.is_file()]
    return paths


def spa_is_stale(webapp: Path) -> bool:
    """True when webapp/dist is missing or older than any of its build inputs.

    A checkout that moves forward (git sets the mtime of every file it
    rewrites) leaves an older dist behind; serving that would capture
    screenshots of a SPA that no longer matches the source. The check is
    mtime-based only: an mtime-preserving restore (rsync -a, cp -p, tar)
    that leaves every input older than dist goes unnoticed.
    """
    index = webapp / "dist" / "index.html"
    if not index.exists():
        return True
    built = index.stat().st_mtime
    return any(_mtime(p) > built for p in _spa_inputs(webapp))


def _mtime(path: Path) -> float:
    # A dangling or looping symlink has no target to stat; it can't be a build input.
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def ensure_spa_built() -> None:
    """The server serves webapp/dist; (re)build it when missing or stale."""
    webapp = ROOT / "webapp"
    if not spa_is_stale(webapp):
        return
    print("[demo] building the SPA (webapp/dist)...")
    subprocess.run(["npm", "--prefix", str(webapp), "run", "build"], check=True)


@dataclass
class Demo:
    demo_dir: Path
    url: str
    server: subprocess.Popen[bytes]
    keep: bool

    def stop(self) -> None:
        if self.server.poll() is None:
            # The server is stopped by name, because it shares the demo's
            # process group rather than leading one of its own (see
            # start_demo). It can exit between poll() and terminate() (e.g.
            # it lost a port race); a ProcessLookupError then must not mask
            # the real error.
            try:
                self.server.terminate()
                try:
                    self.server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.server.kill()
            except ProcessLookupError:
                # Already exited on its own; wait() below just reaps it.
                pass
            self.server.wait()
        if self.keep:
            print(f"[demo] kept {self.demo_dir}")
            return
        shutil.rmtree(self.demo_dir, ignore_errors=True)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it just is not ours to signal
    return True


def clean_stale_scratch_dirs() -> None:
    """Remove the scratch dirs of demos that are no longer running.

    A demo that is stopped with SIGKILL cannot clean up after itself, and
    that is how a supervisor stops it: Playwright's web server runner kills
    the whole process group with SIGKILL. The demo's own pid tells a live
    demo from a dead one, so a scratch dir left behind by a killed demo does
    not pile up in the temp directory.
    """
    for candidate in Path(tempfile.gettempdir()).glob("runway-demo-*"):
        try:
            pid = int((candidate / "pid").read_text().strip())
        except (OSError, ValueError):
            continue  # not one of ours, or one that was never finished
        if _pid_is_alive(pid):
            continue  # a live demo owns it
        shutil.rmtree(candidate, ignore_errors=True)


def start_demo(*, keep: bool = False, quiet: bool = False) -> Demo:
    assert_ports_free([DEMO_PORT])
    ensure_spa_built()

    clean_stale_scratch_dirs()
    demo_dir = Path(tempfile.mkdtemp(prefix="runway-demo-"))
    # The pid tells a later demo that this one is gone, so a demo that was
    # killed with SIGKILL does not leave its scratch dir behind forever.
    (demo_dir / "pid").write_text(str(os.getpid()))
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
            # The server stays in the demo's own process group on purpose.
            # A supervisor that stops the demo -- Playwright's web server
            # runner kills the whole group with SIGKILL -- then takes the
            # server with it. A server in a session of its own outlived the
            # demo, went on holding the port, and went on holding the demo's
            # stdout open, so the supervisor waited for a pipe that never
            # closed and the run never ended. The demo stops the server by
            # name instead; see Demo.stop().
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
    # The handler is installed BEFORE the demo starts, not after it is up.
    # A supervisor that stops the demo with SIGTERM would otherwise kill
    # this process without a teardown while it was still building the SPA,
    # seeding the database or waiting for the server -- the whole
    # webServer.timeout budget -- and the teardown below would never run.
    # Installing the handler first means the signal always unwinds through
    # it: `sys.exit` raises SystemExit, which the `except BaseException` in
    # start_demo() catches and cleans up after, and which the `finally`
    # below turns into a stopped server.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
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
