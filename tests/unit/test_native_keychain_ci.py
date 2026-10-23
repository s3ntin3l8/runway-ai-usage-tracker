"""Nonnative deadline/cleanup/report tests for the hosted-only probe supervisor."""

import json
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="hosted macOS supervisor uses POSIX process groups"
)


@pytest.fixture(autouse=True)
def hosted_environment(monkeypatch):
    for name, value in {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "macOS",
        "RUNWAY_NATIVE_SYNTHETIC": "1",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    "outcome", ["timeout", "start_failed", "interrupted", "exited_early", "success"]
)
def test_supervisor_saves_report_and_reaps_process_group(monkeypatch, tmp_path, outcome):
    from tests import native_keychain_ci as runner

    report_path = tmp_path / "report.json"
    killed, waits = [], []

    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(["private-command"], timeout)
            if outcome == "interrupted":
                raise KeyboardInterrupt("private-interruption")
            if outcome == "success":
                runner.save_report(
                    report_path,
                    {
                        "matrix_complete": True,
                        "candidate_found": False,
                        "cases": [{}],
                    },
                )
                return 0
            return 66
        return 0

    def start(command, **kwargs):
        assert command[1].endswith("tests/native_keychain_public_update_probe.py")
        assert command[2:] == ["--isolated-synthetic-only", "--report", str(report_path)]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        if outcome == "start_failed":
            raise OSError("private-start-failure")
        return SimpleNamespace(pid=12345, wait=wait)

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    code = runner.supervise(report_path)
    result = json.loads(report_path.read_text())
    assert code == (0 if outcome == "success" else 1)
    assert result["matrix_complete"] is (outcome == "success")
    assert result["candidate_found"] is False
    assert "private-" not in json.dumps(result.get("supervisor_failure"))
    if outcome != "start_failed":
        assert killed == [(12345, signal.SIGKILL)]
        assert waits == [90, 5]
    else:
        assert killed == []


def test_deadline_preserves_prior_sanitized_progress_but_never_promotes_candidate(
    monkeypatch, tmp_path
):
    from tests import native_keychain_ci as runner

    path = tmp_path / "report.json"

    def wait(timeout):
        if timeout == 90:
            runner.save_report(
                path,
                {
                    "phase": "case",
                    "active_case": "data-only",
                    "matrix_complete": False,
                    "candidate_found": True,
                    "cases": [{"stages": ["before_update"]}],
                },
            )
            raise subprocess.TimeoutExpired(["private-command"], timeout)
        return 0

    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=12345, wait=wait)
    )
    monkeypatch.setattr(runner.os, "killpg", lambda *a: None)
    assert runner.supervise(path) == 1
    result = json.loads(path.read_text())
    assert result["supervisor_failure"] == "process_group_timed_out"
    assert result["cases"] == [{"stages": ["before_update"]}]
    assert result["matrix_complete"] is False
    assert result["candidate_found"] is False


def test_supervisor_refuses_personal_machine_before_starting_child(monkeypatch, tmp_path):
    from tests import native_keychain_ci as runner

    monkeypatch.delenv("RUNWAY_NATIVE_SYNTHETIC")
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *a, **k: pytest.fail("no subprocess outside CI")
    )
    path = tmp_path / "report.json"
    assert runner.supervise(path) == 1
    assert json.loads(path.read_text())["blocked_reason"] == "ephemeral_macos_ci_required"


@pytest.mark.parametrize("primary", ["timeout", "interrupted"])
@pytest.mark.parametrize("cleanup", ["kill", "reap"])
def test_cleanup_failure_retains_primary_cause(monkeypatch, tmp_path, primary, cleanup):
    from tests import native_keychain_ci as runner

    def wait(timeout):
        if timeout == 90:
            if primary == "timeout":
                raise subprocess.TimeoutExpired(["private-detail"], timeout)
            raise KeyboardInterrupt("private-detail")
        if cleanup == "reap":
            raise subprocess.TimeoutExpired(["private-detail"], timeout)
        return 0

    def kill(*args):
        if cleanup == "kill":
            raise OSError("private-detail")

    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=12345, wait=wait)
    )
    monkeypatch.setattr(runner.os, "killpg", kill)
    path = tmp_path / "report.json"
    assert runner.supervise(path) == 1
    result = json.loads(path.read_text())
    assert result["supervisor_failure"] == (
        "process_group_timed_out" if primary == "timeout" else "interrupted"
    )
    assert result["supervisor_cleanup_failed"] is True
    assert "private-detail" not in path.read_text()


def test_child_preflight_block_is_not_reported_as_supervisor_failure(monkeypatch, tmp_path):
    from tests import native_keychain_ci as runner

    path = tmp_path / "report.json"

    def wait(timeout):
        if timeout == 90:
            runner.save_report(
                path,
                {
                    "blocked_reason": "security_cli_active",
                    "matrix_complete": False,
                    "candidate_found": False,
                    "cases": [],
                },
            )
            return 1
        return 0

    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=12345, wait=wait)
    )
    monkeypatch.setattr(runner.os, "killpg", lambda *a: None)
    assert runner.supervise(path) == 1
    result = json.loads(path.read_text())
    assert result["blocked_reason"] == "security_cli_active"
    assert result["child_exit_code"] == 1
    assert "supervisor_failure" not in result
