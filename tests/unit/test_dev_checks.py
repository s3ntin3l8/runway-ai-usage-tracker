"""Portable gate commands retain failures, UTF-8 and the canonical npm graph."""

import os
import subprocess
from pathlib import Path

import pytest

from scripts import dev_checks as dc


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

    def check(task, extra):
        if task == "gate":
            return original(task, extra)
        calls.append(task)
        if task == "test":
            raise subprocess.CalledProcessError(1, ["test"])
        return None

    monkeypatch.setattr(dc, "check", check)
    with pytest.raises(subprocess.CalledProcessError):
        dc.check("gate", [])
    assert calls == ["lint", "test"]


def test_install_uses_lockfile_and_both_hooks(monkeypatch):
    commands = []
    monkeypatch.setattr(dc, "execute", commands.append)
    monkeypatch.setattr(dc, "npm_command", lambda: ["node", "npm-cli.js"])
    dc.check("install", [])
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
