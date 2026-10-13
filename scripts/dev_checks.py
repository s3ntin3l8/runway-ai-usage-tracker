"""Run the contributor checks on Windows, macOS and Linux without a shell or Make."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def execute(command: list[str], *, isolated_paths: dict[str, str] | None = None) -> None:
    env = {**os.environ, "PYTHONUTF8": "1"}
    if isolated_paths is not None:
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
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def check(task: str, extra: list[str]) -> None:
    python = str(venv_python())
    if task == "install":
        if not venv_python().is_file():
            execute([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        execute([python, "-m", "pip", "install", "--upgrade", "pip"])
        execute([python, "-m", "pip", "install", "-r", "requirements-dev.txt"])
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
        # A unique owned root avoids old pytest roots with another user's Windows ACLs.
        with tempfile.TemporaryDirectory(prefix="runway-pytest-") as tmp:
            args = ["tests/unit/", "-q"] if task == "unit" else []
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
            }
            execute(
                [python, "-m", "pytest", *args, "--basetemp", str(root / "tests"), *extra],
                isolated_paths=isolated_paths,
            )
    elif task == "web-test":
        execute([*npm_command(), "--prefix", "webapp", "test", *(["--", *extra] if extra else [])])
    elif task == "gate":
        for step in ("lint", "test", "web-test"):
            check(step, [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "task",
        choices=("install", "install-hooks", "lint", "mypy", "test", "unit", "web-test", "gate"),
    )
    args, extra = parser.parse_known_args()
    if extra and args.task not in {"test", "unit", "web-test"}:
        parser.error("additional arguments are supported only for test commands")
    try:
        check(args.task, extra)
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
