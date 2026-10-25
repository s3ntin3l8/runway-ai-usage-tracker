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
import contextlib
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

try:  # POSIX only; the demo itself is POSIX-only (it signals its own group).
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from scripts.demo.env import assert_demo_env, assert_ports_free, demo_env

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "bin" / "python"
DEMO_PORT = int(os.environ.get("DEMO_PORT", "3965"))

# The marker a demo leaves in its scratch dir when it was told to keep it.
KEEP_MARKER = "keep"

# The lock a running demo holds on its scratch dir.
#
# The lock, not the pid, is what says a demo is still there. A pid cannot:
# pids are reused, so the pid of a demo that died last week can belong to an
# unrelated process today, and a pid that is alive can also belong to a demo
# that has been running for days -- neither is evidence of anything. A lock
# is held by the process that took it and released when that process dies.
LOCK_FILE = "lock"

# How old a scratch dir has to be before the sweep takes one that was never
# claimed. A demo that dies between making the dir and claiming it leaves a
# dir nothing can be read from, and a demo that is starting right now has
# just made its dir.
STALE_AFTER_SECONDS = 24 * 60 * 60


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
    # The open lock on the scratch dir. It is held for as long as the demo
    # runs, so a later demo can tell that this one is still there.
    lock_fd: int | None = None

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
            # The dir is the operator's now. The marker tells a later demo
            # that this one was kept on purpose, so the sweep does not take
            # it for a leftover: a kept demo's process is gone the moment it
            # exits, and the lock alone would not tell the two apart.
            _mark_kept(self.demo_dir)
            print(f"[demo] kept {self.demo_dir}")
        else:
            shutil.rmtree(self.demo_dir, ignore_errors=True)
        # The lock is released LAST. Until it is, the sweep sees a dir that
        # is still claimed, and a dir that has just lost its lock, its
        # marker and its contents is exactly what the sweep takes.
        self._release_lock()

    def _release_lock(self) -> None:
        if self.lock_fd is None:
            return
        # Releasing the lock is a best-effort courtesy to the next sweep: a
        # close that fails has already lost the fd, and the process is about
        # to end anyway, which releases the lock with it.
        with contextlib.suppress(OSError):
            os.close(self.lock_fd)
        self.lock_fd = None


def _mark_kept(demo_dir: Path) -> None:
    """Say that the dir is the operator's, so the sweep leaves it alone."""
    try:
        (demo_dir / KEEP_MARKER).write_text("kept")
    except OSError as e:
        # The dir is kept whatever we can write into it, and this runs from
        # a teardown: an error here would replace the exception already in
        # flight and turn a clean stop into a traceback.
        print(f"[demo] could not mark {demo_dir} as kept: {e}")


def _claim_demo_dir(demo_dir: Path) -> int | None:
    """Lock the scratch dir for as long as this process lives.

    The lock is what a later demo reads: it can take the lock only when
    this demo is gone.
    """
    if fcntl is None:  # pragma: no cover - Windows
        return None
    fd = os.open(demo_dir / LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    return fd


def _dir_is_claimed(demo_dir: Path) -> bool:
    """True when a demo still holds the scratch dir.

    A demo that died released its lock, and so did one that was killed with
    SIGKILL: the lock belongs to the process, not to the file.
    """
    if fcntl is None:  # pragma: no cover - Windows
        return False
    lock_path = demo_dir / LOCK_FILE
    if not lock_path.exists():
        return False  # never claimed: the demo died before it could
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return False  # the demo removed it: never claimed
    except OSError:
        # The lock cannot be read (no permission, too many open files, a
        # broken disk). That is not evidence that nobody owns the dir -- it
        # is evidence that we cannot tell -- so the dir is left alone.
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True  # held: a live demo owns this dir
    finally:
        os.close(fd)
    return False


def _age_of(path: Path) -> float:
    """Seconds since the path was last changed."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return 0.0


def clean_stale_scratch_dirs() -> None:
    """Remove the scratch dirs of demos that are no longer running.

    A demo that is stopped with SIGKILL cannot clean up after itself, and
    that is how a supervisor stops it: Playwright's web server runner kills
    the whole process group with SIGKILL. The demo's lock tells a live demo
    from a dead one, so a scratch dir left behind by a killed demo does not
    pile up in the temp directory.

    Two things the sweep leaves alone:

    - A dir that was kept on purpose (`--keep`) is the operator's, not ours
      to delete. It says so itself.
    - A dir that was never claimed is a demo that died between making the
      dir and locking it, or a demo that is starting right now. Only an old
      one is swept.
    """
    for candidate in Path(tempfile.gettempdir()).glob("runway-demo-*"):
        if (candidate / KEEP_MARKER).exists():
            continue  # a kept demo is deliberate
        if _dir_is_claimed(candidate):
            continue  # a live demo owns it
        if not (candidate / LOCK_FILE).exists() and _age_of(candidate) < STALE_AFTER_SECONDS:
            continue  # never claimed, and too young to be a leftover
        shutil.rmtree(candidate, ignore_errors=True)


def start_demo(*, keep: bool = False, quiet: bool = False) -> Demo:
    assert_ports_free([DEMO_PORT])
    ensure_spa_built()

    clean_stale_scratch_dirs()
    demo_dir = Path(tempfile.mkdtemp(prefix="runway-demo-"))
    # The lock is taken before the dir holds anything else. It is what tells
    # a later demo that this one is still running, so a demo that was killed
    # with SIGKILL does not leave its scratch dir behind for ever. A demo
    # that dies between making the dir and locking it leaves a dir nothing
    # can be read from, and the sweep takes one of those only once it is
    # old.
    lock_fd = _claim_demo_dir(demo_dir)
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
        return Demo(demo_dir=demo_dir, url=url, server=server, keep=keep, lock_fd=lock_fd)
    except BaseException:
        if server is not None:
            Demo(demo_dir=demo_dir, url="", server=server, keep=keep, lock_fd=lock_fd).stop()
        else:
            if keep:
                # The dir is the operator's, and a failed start is exactly
                # when they want it: mark it kept, or the next sweep takes
                # it for a leftover.
                _mark_kept(demo_dir)
            if lock_fd is not None:
                os.close(lock_fd)
            if not keep:
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
