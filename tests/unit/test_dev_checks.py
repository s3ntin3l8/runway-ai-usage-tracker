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
    # Only the pytest step is told the worker count; lint, web-lint and
    # web-test do not run the suite.
    assert seen == [("lint", 0), ("test", 4), ("web-lint", 0), ("web-test", 0)]


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


@pytest.mark.parametrize(
    ("requirements_set", "expected"),
    [
        ("all", "requirements-dev.txt"),
        ("test", "requirements-test.txt"),
        ("lint", "requirements-lint.txt"),
    ],
)
def test_the_requirements_set_picks_the_file_to_install(monkeypatch, requirements_set, expected):
    """`--set` picks which dependency set `install-python` installs.

    A job that only runs tests must not install the lint tooling, and a job
    that only lints must not install the suite's packages: the lint tree is
    about thirty packages the suite never touches, and unpacking them onto
    the Windows filesystem is a third of the job's install step.
    """
    commands = []
    monkeypatch.setattr(dc, "execute", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(sys, "argv", ["dev_checks.py", "install-python", "--set", requirements_set])
    assert dc.main() == 0
    assert commands[-1][-2:] == ["-r", expected]


@pytest.mark.parametrize("task", ["install", "lint"])
def test_the_requirements_set_is_rejected_for_tasks_that_do_not_install(monkeypatch, task):
    """`--set` is meaningless for a task that installs nothing.

    ``install`` is the one that separates the old code from the new: it used
    to accept ``--set`` and then die in ``install-hooks``, which needs
    ``pre-commit`` -- a package only the lint set carries. ``lint`` was
    always rejected, so it passes either way; it is here so that the rule
    is pinned for both.
    """
    monkeypatch.setattr(sys, "argv", ["dev_checks.py", task, "--set", "test"])
    with pytest.raises(SystemExit):
        dc.main()


def test_install_installs_the_union(monkeypatch):
    """`make install` is the full set, so the hooks work afterwards.

    The hooks need `pre-commit`, which is in the lint set -- so `install`
    cannot take a narrower set than the union.
    """
    commands = []
    monkeypatch.setattr(dc, "execute", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(dc, "npm_command", lambda: ["node", "npm-cli.js"])
    dc.check("install", [])
    assert ["-r", "requirements-dev.txt"] in [command[-2:] for command in commands]


def test_install_uses_lockfile_and_both_hooks(monkeypatch):
    commands = []
    installed = []
    dc.venv_python().unlink()

    def execute(command):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            dc.venv_python().touch()

    monkeypatch.setattr(dc, "execute", execute)
    monkeypatch.setattr(dc, "npm_command", lambda: ["node", "npm-cli.js"])
    monkeypatch.setattr(dc, "install_git_hooks", lambda: installed.append(True))
    dc.check("install", [])
    assert commands[0][1:3] == ["-m", "venv"]
    assert ["node", "npm-cli.js", "--prefix", "webapp", "ci"] in commands
    # The hooks are no longer installed by `pre-commit install`, which wrote
    # a script with the installing interpreter baked in.
    assert not any("pre_commit" in part for command in commands for part in command)
    assert installed == [True]


def test_install_hooks_copies_the_tracked_script(tmp_path, monkeypatch):
    """The installed hook is the repository's own script, with no baked path."""
    hooks = tmp_path / "hooks"
    monkeypatch.setattr(dc, "git_hooks_dir", lambda: hooks)
    dc.install_git_hooks()
    for hook_type in ("pre-commit", "pre-push"):
        installed = hooks / hook_type
        assert installed.is_file()
        source = installed.read_text(encoding="utf-8")
        assert source == dc.HOOK_SCRIPT.read_text(encoding="utf-8")
        assert "INSTALL_PYTHON" not in source
        assert str(dc.REPO_ROOT) not in source


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
    ["lint", "mypy", "test", "unit", "test-shard", "gate", "record-shard-weights"],
)
def test_missing_venv_reports_install_before_running_commands(tmp_path, monkeypatch, task):
    dc.venv_python().unlink()
    monkeypatch.setattr(dc, "execute", lambda *a, **k: pytest.fail("check ran without .venv"))
    with pytest.raises(RuntimeError, match=r"python scripts/dev_checks.py install first"):
        dc.check(task, [])


def test_install_hooks_does_not_need_a_venv(monkeypatch, tmp_path):
    """Installing the hooks copies a file; it does not run a check."""
    dc.venv_python().unlink()
    monkeypatch.setattr(dc, "git_hooks_dir", lambda: tmp_path / "hooks")
    monkeypatch.setattr(dc, "execute", lambda *a, **k: pytest.fail("install-hooks ran a command"))
    dc.check("install-hooks", [])
    assert (tmp_path / "hooks" / "pre-commit").is_file()


def test_the_suite_runs_without_gits_variables(monkeypatch):
    """No ``GIT_*`` variable survives into the environment the suite runs in.

    The filter is by prefix, not by name. Git propagates arbitrary ``-c``
    configuration to a hook as ``GIT_CONFIG_PARAMETERS``, and sets a dozen
    more variables besides, so an allowlist of names would be a list to keep
    in step with git. ``GIT_CONFIG_PARAMETERS`` and ``GIT_AUTHOR_NAME`` are
    not in any list this repository has had, which is what makes them the
    interesting ones here: a change back to a named allowlist would leave
    them behind and fail this test.
    """
    for name in (
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_PARAMETERS",
        "GIT_AUTHOR_NAME",
        "GIT_EXEC_PATH",
    ):
        monkeypatch.setenv(name, "value")
    monkeypatch.setenv("NOT_A_GIT_VARIABLE", "kept")

    captured: dict[str, str] = {}

    def capture(command, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(dc.subprocess, "run", capture)
    dc.execute(["synthetic-check"])

    assert not [name for name in captured if name.startswith("GIT_")], captured
    assert captured["NOT_A_GIT_VARIABLE"] == "kept"
