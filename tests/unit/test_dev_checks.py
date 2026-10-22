"""Portable gate commands retain failures, UTF-8 and the canonical npm graph."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import dev_checks as dc


@pytest.fixture(autouse=True)
def owned_checkout(tmp_path, monkeypatch):
    """Mocked commands use an owned checkout, never the caller's real .venv."""
    monkeypatch.setattr(dc, "ROOT", tmp_path)
    interpreter = dc.venv_python()
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()


def test_command_failure_is_not_hidden(monkeypatch):
    def fail(command, **kwargs):
        assert kwargs["check"] is True
        assert kwargs["cwd"] == dc.ROOT
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        raise subprocess.CalledProcessError(7, command)

    monkeypatch.setattr(dc.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        dc.execute(["synthetic-check"])
    assert exc.value.returncode == 7


def test_gate_stops_after_a_failed_step(monkeypatch):
    calls = []
    original = dc.check

    def check(task, extra, **kwargs):
        if task == "gate":
            return original(task, extra, **kwargs)
        calls.append(task)
        if task == "test":
            raise subprocess.CalledProcessError(1, ["test"])
        return None

    monkeypatch.setattr(dc, "check", check)
    with pytest.raises(subprocess.CalledProcessError):
        dc.check("gate", [])
    assert calls == ["lint", "test"]


def test_gate_forwards_the_worker_count_to_its_steps(monkeypatch):
    """`--jobs` reaches the gate's test step, not just the standalone tasks."""
    seen = []
    original = dc.check

    def check(task, extra, **kwargs):
        if task == "gate":
            return original(task, extra, **kwargs)
        seen.append((task, kwargs.get("jobs", 0)))
        return None

    monkeypatch.setattr(dc, "check", check)
    dc.check("gate", [], jobs=4)
    # Only the pytest step is told the worker count; lint and web-test do
    # not run the suite.
    assert seen == [("lint", 0), ("test", 4), ("web-test", 0)]


@pytest.mark.parametrize(
    ("jobs", "expected"),
    [
        (4, ["-n", "4", "--dist", "loadfile"]),
        (1, []),
        (0, []),
        (-1, []),
    ],
)
def test_parallel_args_pins_the_worker_grouping(jobs, expected):
    """`--dist loadfile` is load-bearing: the default would split a module.

    The default `--dist load` hands individual tests to workers, so a
    module-scoped fixture and a module's import-time setup would end up
    spread across workers. `loadfile` keeps every module in one worker.
    """
    assert dc.parallel_args(jobs) == expected


def test_jobs_is_rejected_for_tasks_that_do_not_run_tests(monkeypatch):
    """`--jobs` is meaningless for a task that never starts pytest."""
    monkeypatch.setattr(sys, "argv", ["dev_checks.py", "lint", "--jobs", "4"])
    with pytest.raises(SystemExit):
        dc.main()


def test_install_uses_lockfile_and_both_hooks(monkeypatch):
    commands = []
    dc.venv_python().unlink()

    def execute(command):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            dc.venv_python().touch()

    monkeypatch.setattr(dc, "execute", execute)
    monkeypatch.setattr(dc, "npm_command", lambda: ["node", "npm-cli.js"])
    dc.check("install", [])
    assert commands[0][1:3] == ["-m", "venv"]
    assert ["node", "npm-cli.js", "--prefix", "webapp", "ci"] in commands
    assert commands[-2][-3:] == ["-m", "pre_commit", "install"]
    assert commands[-1][-2:] == ["--hook-type", "pre-push"]


def test_tests_get_an_owned_root_and_keep_pytest_arguments(monkeypatch):
    commands = []
    monkeypatch.setattr(dc, "execute", commands.append)
    paths = []

    def capture(command, **kwargs):
        commands.append(command)
        paths.append(kwargs["isolated_paths"])

    monkeypatch.setattr(dc, "execute", capture)
    dc.check("test", ["tests/unit/test_settings.py"])
    command = commands[0]
    assert command[:3] == [str(dc.venv_python()), "-m", "pytest"]
    assert command[-1] == "tests/unit/test_settings.py"
    temp = Path(command[command.index("--basetemp") + 1])
    assert temp.parent.name.startswith("runway-pytest-")
    assert not temp.parent.exists()
    assert paths[0]["HOME"] == paths[0]["USERPROFILE"]
    for name in ("APPDATA", "LOCALAPPDATA", "RUNWAY_CONFIG_DIR"):
        assert Path(paths[0][name]).is_relative_to(temp.parent)
    # The root itself is exported too, so the suite can give each xdist
    # worker its own home and config directory (tests/conftest.py).
    assert Path(paths[0]["TEST_ISOLATION_ROOT"]) == temp.parent


def test_missing_npm_reports_setup_requirement(monkeypatch):
    monkeypatch.setattr(dc.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Install Node.js with npm"):
        dc.npm_command()


def test_venv_interpreter_matches_native_platform():
    suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    assert dc.venv_python() == dc.ROOT / ".venv" / suffix


def test_web_test_arguments_are_forwarded_to_vitest(monkeypatch):
    commands = []
    monkeypatch.setattr(dc, "execute", commands.append)
    monkeypatch.setattr(dc, "npm_command", lambda: ["node", "npm-cli.js"])
    dc.check("web-test", ["--reporter=json"])
    assert commands == [
        ["node", "npm-cli.js", "--prefix", "webapp", "test", "--", "--reporter=json"]
    ]


def test_isolated_checks_remove_inherited_database_and_login_paths(monkeypatch):
    for name in (
        "DATABASE_PATH",
        "GITHUB_OAUTH_PATH",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "HERMES_HOME",
    ):
        monkeypatch.setenv(name, "operator-private-path")

    def capture(command, **kwargs):
        env = kwargs["env"]
        assert env["RUNWAY_CONFIG_DIR"] == "isolated-config"
        for name in (
            "DATABASE_PATH",
            "GITHUB_OAUTH_PATH",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "HERMES_HOME",
        ):
            assert name not in env

    monkeypatch.setattr(dc.subprocess, "run", capture)
    dc.execute(["synthetic-check"], isolated_paths={"RUNWAY_CONFIG_DIR": "isolated-config"})


def test_test_shard_runs_the_suite_without_the_other_shards(monkeypatch):
    commands = []
    monkeypatch.setattr(dc, "suite_files", lambda: ["tests/unit/a.py", "tests/unit/b.py"])
    monkeypatch.setattr(dc, "shard_files", lambda shard_id, shards, modules: [modules[0]])
    monkeypatch.setattr(dc, "execute", lambda command, **kwargs: commands.append(command))
    dc.check("test-shard", [], shard_id=1, shards=2)
    command = commands[0]
    assert command[:4] == [str(dc.venv_python()), "-m", "pytest", "tests/"]
    assert "--deselect=tests/unit/b.py" in command
    assert "--deselect=tests/unit/a.py" not in command


def test_install_python_skips_the_node_dependencies(monkeypatch):
    commands = []
    dc.venv_python().unlink()

    def execute(command):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            dc.venv_python().touch()

    monkeypatch.setattr(dc, "execute", execute)
    monkeypatch.setattr(dc, "npm_command", lambda: pytest.fail("npm is not needed"))
    dc.check("install-python", [])
    assert commands[0][1:3] == ["-m", "venv"]
    assert not any("npm" in " ".join(command) for command in commands)


def test_shard_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("RUNWAY_PYTEST_SHARD_ID", "3")
    monkeypatch.setenv("RUNWAY_PYTEST_SHARDS", "6")
    seen = {}

    def check(task, extra, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(dc, "check", check)
    monkeypatch.setattr(sys, "argv", ["dev_checks.py", "test-shard"])
    assert dc.main() == 0
    assert seen["shard_id"] == 3
    assert seen["shards"] == 6


@pytest.mark.parametrize(
    "task",
    ["install-hooks", "lint", "mypy", "test", "unit", "test-shard", "gate", "record-shard-weights"],
)
def test_missing_venv_reports_install_before_running_commands(tmp_path, monkeypatch, task):
    dc.venv_python().unlink()
    monkeypatch.setattr(dc, "execute", lambda *a, **k: pytest.fail("check ran without .venv"))
    with pytest.raises(RuntimeError, match=r"python scripts/dev_checks.py install first"):
        dc.check(task, [])


def test_the_system_python_switch_skips_the_venv(monkeypatch):
    """A CI job can use the runner's Python instead of building a .venv.

    A runner is a fresh virtual machine per job, so its interpreter is
    already isolated. Building a .venv on top of it costs a venv creation and
    a second pip upgrade on every run -- a third of a Windows job, for a
    dependency install that is already cached.

    Locally the .venv is the point: it keeps the contributor's interpreter
    untouched. So the switch is off unless a job asks for it.
    """
    monkeypatch.delenv("RUNWAY_SYSTEM_PYTHON", raising=False)
    assert not dc.use_system_python()

    commands: list[list[str]] = []
    monkeypatch.setattr(dc, "execute", lambda command, **kwargs: commands.append(command))
    dc.check("install-python", [])
    # The repository's .venv, not the interpreter the job is already running.
    assert [command[0] for command in commands] == [str(dc.venv_python())] * 2, commands

    commands.clear()
    monkeypatch.setenv("RUNWAY_SYSTEM_PYTHON", "1")
    assert dc.use_system_python()
    dc.check("install-python", [])
    # The current interpreter, and no venv created on top of it.
    assert [command[0] for command in commands] == [sys.executable] * 2, commands
    assert [command[1:3] for command in commands] == [["-m", "pip"], ["-m", "pip"]], commands
