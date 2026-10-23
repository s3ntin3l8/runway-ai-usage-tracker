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

#: The dependency sets, by name. The suite needs one set, the static gate
#: another, and a checkout that wants to do both needs the union -- so a job
#: that only runs tests installs about 30 packages fewer than one that
#: installs everything, which is a third of a Windows job's install step.
REQUIREMENTS_FILES = {
    "all": "requirements-dev.txt",
    "test": "requirements-test.txt",
    "lint": "requirements-lint.txt",
}


def venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


#: The repository root, captured before anything can repoint ROOT. The git
#: helpers below have to run in the real checkout even when a caller has
#: pointed ROOT somewhere else.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The hook script this repository installs. It resolves its interpreter at run
#: time, so a hook installed from a worktree keeps working when that worktree
#: is removed -- see ``scripts/hooks/git-hook.sh``.
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "git-hook.sh"
HOOK_TYPES = ("pre-commit", "pre-push")


def git_hooks_dir() -> Path:
    """The repository's shared hooks directory.

    Every worktree writes to the same two hook files: ``git rev-parse
    --git-path hooks`` resolves to the *common* git directory, not to the
    worktree's own.
    """
    completed = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    hooks = Path(completed.stdout.strip())
    return hooks if hooks.is_absolute() else REPO_ROOT / hooks


def install_git_hooks() -> None:
    """Install this repository's hook script into the shared hooks directory.

    ``pre-commit install`` writes a script that pins the interpreter that ran
    it, so installing from a worktree breaks every commit in every worktree
    once that worktree is removed. This script resolves its interpreter when
    it runs instead, so where it was installed from does not matter.
    """
    hooks = git_hooks_dir()
    hooks.mkdir(parents=True, exist_ok=True)
    source = HOOK_SCRIPT.read_text(encoding="utf-8")
    for hook_type in HOOK_TYPES:
        target = hooks / hook_type
        target.write_text(source, encoding="utf-8", newline="\n")
        target.chmod(0o755)
    print(f"installed {HOOK_SCRIPT.name} as {', '.join(HOOK_TYPES)} in {hooks}")
    print("  the hook resolves its interpreter when it runs, so it survives the")
    print("  worktree it was installed from being removed.")
    print("  a worktree still needs its own .venv for the mypy and pytest hooks:")
    print("    python scripts/dev_checks.py install-python")


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

    The paths exported here are the serial run's. ``TEST_ISOLATION_ROOT``
    carries the root itself so the suite can specialise it per xdist worker:
    the four workers of a parallel run must not share one app database file,
    or two tests writing to the app engine at the same time contend on it.
    That specialisation happens in the worker (see ``tests/conftest.py``),
    because the worker id is only known there, so on the parallel path the
    exported HOME and config directory are the fallback the conftest
    re-derives per worker -- not the paths the workers end up using.
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
    requirements_set: str = "all",
) -> None:
    interpreter = venv_python()
    if (
        task not in {"install", "install-python", "install-hooks", "web-test", "web-lint"}
        and not interpreter.is_file()
    ):
        raise RuntimeError("Run python scripts/dev_checks.py install first to create .venv")
    python = str(interpreter)
    if task == "install-python":
        if not venv_python().is_file():
            execute([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        execute([python, "-m", "pip", "install", "--upgrade", "pip"])
        execute([python, "-m", "pip", "install", "-r", REQUIREMENTS_FILES[requirements_set]])
    elif task == "install":
        check("install-python", [])
        execute([*npm_command(), "--prefix", "webapp", "ci"])
        check("install-hooks", [])
    elif task == "install-hooks":
        install_git_hooks()
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
        #
        # The measurement runs with the same worker count the shards use, so
        # the numbers describe the runs they plan: a module's cost inside a
        # four-worker shard is not its serial cost.
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
                    *parallel_args(jobs),
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
    elif task == "web-lint":
        # The frontend's lint. The shared CI workflow runs it inside the
        # required `test-frontend / lint-and-test` job, so a lint error blocks
        # a merge -- which means the local gate has to run it too, or
        # `make lint && make test && make web-test` can be green while CI is
        # red.
        # `npm lint` is not a builtin the way `npm test` is, so the
        # script has to be named explicitly.
        execute([*npm_command(), "--prefix", "webapp", "run", "lint"])
    elif task == "gate":
        # Only the pytest step runs the suite, so only it is told how many
        # workers to use. `lint` runs ruff and mypy; `web-lint` runs eslint;
        # `web-test` runs vitest, which has its own pool knob.
        check("lint", [])
        check("test", [], jobs=jobs)
        check("web-lint", [])
        check("web-test", [])


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
            "web-lint",
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
    parser.add_argument(
        "--set",
        dest="requirements_set",
        choices=tuple(REQUIREMENTS_FILES),
        default="all",
        help="which dependency set to install: 'test' for the suite's, "
        "'lint' for the static gate's, 'all' for both. Only "
        "'install-python' takes it -- 'install' is the full setup, hooks "
        "included",
    )
    args, extra = parser.parse_known_args()
    if extra and args.task not in {"test", "unit", "test-shard", "test-windows", "web-test"}:
        parser.error("additional arguments are supported only for test commands")
    if args.requirements_set != "all" and args.task != "install-python":
        parser.error("--set is supported only for the install-python task")
    if args.jobs is not None and args.task not in {
        "test",
        "unit",
        "test-shard",
        "test-windows",
        "record-shard-weights",
        "gate",
    }:
        parser.error(
            "--jobs is supported only for the test, unit, test-shard, test-windows, "
            "record-shard-weights and gate tasks"
        )
    shard_id = args.shard_id
    if shard_id is None:
        shard_id = int(os.environ.get("RUNWAY_PYTEST_SHARD_ID", "1"))
    shards = args.shards
    if shards is None:
        shards = int(os.environ.get("RUNWAY_PYTEST_SHARDS", str(DEFAULT_SHARDS)))
    jobs = args.jobs
    if jobs is None:
        # The recording has to measure what the shards run, and the shards
        # run four workers -- so the default here is four too, and the same
        # numbers come out whether the table is recorded locally or in CI.
        default = "4" if args.task == "record-shard-weights" else "0"
        jobs = int(os.environ.get("RUNWAY_TEST_JOBS", default))
    try:
        check(
            args.task,
            extra,
            shard_id=shard_id,
            shards=shards,
            jobs=jobs,
            requirements_set=args.requirements_set,
        )
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
