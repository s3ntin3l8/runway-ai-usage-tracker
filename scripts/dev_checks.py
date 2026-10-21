"""Run the contributor checks on Windows, macOS and Linux without a shell or Make."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pytest_shard import (  # noqa: E402
    DEFAULT_SHARDS,
    parse_durations,
    shard_deselections,
    shard_files,
    suite_files,
    weights_path,
    write_weights,
)


def venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def npm_command() -> list[str]:
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("Install Node.js with npm and put it on PATH before running web checks")
    path = Path(npm)
    if os.name == "nt" and path.suffix.lower() == ".cmd":
        # Run npm's JS entry directly: no cmd.exe quoting or shell interpolation.
        candidates = (
            path.parent / "node_modules/npm/bin/npm-cli.js",
            path.parent.parent / "npm/bin/npm-cli.js",
        )
        node = shutil.which("node")
        for cli in candidates:
            if node and cli.is_file():
                return [node, str(cli)]
        raise RuntimeError(
            "Cannot locate npm-cli.js beside npm.cmd; use a standard Node.js install"
        )
    return [npm]


def execute(
    command: list[str],
    *,
    isolated_paths: dict[str, str] | None = None,
    stdout: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    if isolated_paths is not None:
        # Isolate filesystem state, not every runtime setting. Tests requiring
        # default API/CORS/auth settings must clear those inherited variables.
        for name in (
            "DATABASE_PATH",
            "GITHUB_OAUTH_PATH",
            "OPENCODE_DB_PATH",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "HERMES_HOME",
        ):
            env.pop(name, None)
        env.update(isolated_paths)
    if stdout is None:
        return subprocess.run(command, cwd=ROOT, env=env, check=True)
    with stdout.open("wb") as handle:
        return subprocess.run(command, cwd=ROOT, env=env, check=True, stdout=handle)


@contextlib.contextmanager
def isolated_environment() -> Iterator[tuple[Path, dict[str, str]]]:
    """A throwaway HOME/config root so the suite never reads the operator's files.

    Yields the temporary root plus the environment overrides that point HOME,
    APPDATA, the XDG directories and RUNWAY_CONFIG_DIR at it.

    ``TEST_ISOLATION_ROOT`` carries the root itself so the suite can
    specialise it per xdist worker: the four workers of a parallel run must
    not share one app database file, or two tests writing to the app engine
    at the same time contend on it. The specialisation happens in the worker
    (see ``tests/conftest.py``) because the worker id is only known there.
    """
    # A unique owned root avoids old pytest roots with another user's Windows ACLs.
    with tempfile.TemporaryDirectory(prefix="runway-pytest-") as tmp:
        root = Path(tmp)
        home = root / "home"
        config = root / "config"
        home.mkdir()
        config.mkdir()
        isolated_paths = {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData/Roaming"),
            "LOCALAPPDATA": str(home / "AppData/Local"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "RUNWAY_CONFIG_DIR": str(config),
            "TEST_ISOLATION_ROOT": str(root),
        }
        yield root, isolated_paths


def parallel_args(jobs: int) -> list[str]:
    """The pytest arguments that run the suite in ``jobs`` parallel workers.

    ``--dist loadfile`` keeps every test module inside a single worker, so
    a module's module-scoped fixtures and anything it sets up at import time
    stay together. That is the same property ``scripts/pytest_shard.py``
    relies on when it splits the suite across CI runners, and the reason
    in-process parallelism is safe here.
    """
    if jobs <= 1:
        return []
    return ["-n", str(jobs), "--dist", "loadfile"]


def check(
    task: str,
    extra: list[str],
    *,
    shard_id: int = 1,
    shards: int = DEFAULT_SHARDS,
    jobs: int = 0,
) -> None:
    interpreter = venv_python()
    if task not in {"install", "install-python", "web-test"} and not interpreter.is_file():
        raise RuntimeError("Run python scripts/dev_checks.py install first to create .venv")
    python = str(interpreter)
    if task == "install-python":
        if not venv_python().is_file():
            execute([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        execute([python, "-m", "pip", "install", "--upgrade", "pip"])
        execute([python, "-m", "pip", "install", "-r", "requirements-dev.txt"])
    elif task == "install":
        check("install-python", [])
        execute([*npm_command(), "--prefix", "webapp", "ci"])
        check("install-hooks", [])
    elif task == "install-hooks":
        execute([python, "-m", "pre_commit", "install"])
        execute([python, "-m", "pre_commit", "install", "--hook-type", "pre-push"])
    elif task == "lint":
        execute([python, "-m", "ruff", "check", "."])
        execute([python, "-m", "ruff", "format", "--check", "."])
        check("mypy", [])
        execute([python, "-m", "pip_audit", "-r", "requirements.txt"])
    elif task == "mypy":
        execute([python, "-m", "mypy", "."])
    elif task in {"test", "unit"}:
        with isolated_environment() as (root, isolated_paths):
            args = ["tests/unit/", "-q"] if task == "unit" else []
            execute(
                [
                    python,
                    "-m",
                    "pytest",
                    *args,
                    *parallel_args(jobs),
                    "--basetemp",
                    str(root / "tests"),
                    *extra,
                ],
                isolated_paths=isolated_paths,
            )
    elif task == "test-windows":
        # The native Windows gate: only the modules whose subject is the
        # Windows platform. The rest of the suite runs on the Windows runner
        # in the full-suite workflow (main pushes and the nightly run).
        with isolated_environment() as (root, isolated_paths):
            execute(
                [
                    python,
                    "-m",
                    "pytest",
                    "tests/",
                    "-m",
                    "windows",
                    *parallel_args(jobs),
                    "--basetemp",
                    str(root / "tests"),
                    *extra,
                ],
                isolated_paths=isolated_paths,
            )
    elif task == "test-shard":
        # One shard of the suite: whole test modules only, so the fixed-port
        # helpers and tests/conftest.py's import-time setup stay in one process.
        with isolated_environment() as (root, isolated_paths):
            modules = suite_files()
            shard = shard_files(shard_id, shards, modules)
            print(f"Shard {shard_id}/{shards}: {len(shard)} of {len(modules)} test modules")
            for module in shard:
                print(f"  {module}")
            execute(
                [
                    python,
                    "-m",
                    "pytest",
                    "tests/",
                    *shard_deselections(modules, shard),
                    *parallel_args(jobs),
                    "--basetemp",
                    str(root / "tests"),
                    *extra,
                ],
                isolated_paths=isolated_paths,
            )
    elif task == "record-shard-weights":
        # Measure how long every test module takes so the shard planner can
        # balance by runtime instead of by file or test count.
        with isolated_environment() as (root, isolated_paths):
            report = root / "durations.txt"
            print("Running the suite to measure per-module durations ...")
            execute(
                [
                    python,
                    "-m",
                    "pytest",
                    "--durations=0",
                    "-vv",
                    "--basetemp",
                    str(root / "tests"),
                ],
                isolated_paths=isolated_paths,
                stdout=report,
            )
            durations = parse_durations(report.read_text(encoding="utf-8", errors="replace"))
            target = weights_path()
            write_weights(target, durations)
            print(f"Wrote {len(durations)} module durations to {target}")
    elif task == "web-test":
        execute([*npm_command(), "--prefix", "webapp", "test", *(["--", *extra] if extra else [])])
    elif task == "gate":
        for step in ("lint", "test", "web-test"):
            check(step, [], jobs=jobs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "task",
        choices=(
            "install",
            "install-python",
            "install-hooks",
            "lint",
            "mypy",
            "test",
            "test-shard",
            "test-windows",
            "unit",
            "web-test",
            "gate",
            "record-shard-weights",
        ),
    )
    # The shard id and count come from the command line locally and from the
    # environment in CI, where the matrix value is passed through an env var
    # instead of being interpolated into the shell command.
    parser.add_argument("--shard-id", type=int, default=None, help="1-based shard to run")
    parser.add_argument("--shards", type=int, default=None, help="total number of shards")
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="run the suite in this many parallel workers (1 = serial)",
    )
    args, extra = parser.parse_known_args()
    if extra and args.task not in {"test", "unit", "test-shard", "test-windows", "web-test"}:
        parser.error("additional arguments are supported only for test commands")
    if args.jobs is not None and args.task not in {
        "test",
        "unit",
        "test-shard",
        "test-windows",
        "gate",
    }:
        parser.error(
            "--jobs is supported only for the test, unit, test-shard, test-windows and gate tasks"
        )
    shard_id = args.shard_id
    if shard_id is None:
        shard_id = int(os.environ.get("RUNWAY_PYTEST_SHARD_ID", "1"))
    shards = args.shards
    if shards is None:
        shards = int(os.environ.get("RUNWAY_PYTEST_SHARDS", str(DEFAULT_SHARDS)))
    jobs = args.jobs
    if jobs is None:
        jobs = int(os.environ.get("RUNWAY_TEST_JOBS", "0"))
    try:
        check(args.task, extra, shard_id=shard_id, shards=shards, jobs=jobs)
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
