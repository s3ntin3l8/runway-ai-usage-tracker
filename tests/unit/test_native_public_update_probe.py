"""No native frameworks: regressions for failed #773 probe cleanup/reporting."""

import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests import native_claude_keychain_acceptance as acceptance
from tests import native_keychain_public_update_probe as probe


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


def test_cleanup_deletes_only_with_interaction_disabled(monkeypatch, tmp_path):
    state = {"disabled": False, "deletions": 0}

    @contextmanager
    def without_interaction():
        state["disabled"] = True
        try:
            yield
        finally:
            state["disabled"] = False

    def delete(_handle):
        assert state["disabled"], "cleanup must not trigger authorization UI"
        state["deletions"] += 1
        return 0

    directory = tmp_path / "disposable"
    directory.mkdir()
    fixture = object.__new__(acceptance.Fixture)
    fixture.api = SimpleNamespace(
        _without_interaction=without_interaction,
        sec=SimpleNamespace(SecKeychainDelete=delete),
        cf=SimpleNamespace(CFRelease=lambda _handle: None),
    )
    fixture.keychains = [1]
    fixture.refs = []
    fixture.before = {}
    fixture.directory = SimpleNamespace(name=str(directory), cleanup=directory.rmdir)
    monkeypatch.setattr(acceptance, "environment_snapshot", lambda: {})

    fixture.__exit__(None, None, None)

    assert state == {"disabled": False, "deletions": 1}
    assert all(fixture.cleanup.values())


@pytest.mark.parametrize("interrupted", [None, KeyboardInterrupt, SystemExit])
def test_probe_retains_primary_error_when_cleanup_fails(monkeypatch, tmp_path, interrupted):
    snapshot = {
        "default": {"path": "synthetic", "unlocked": False},
        "search_list": [],
    }

    class BrokenCleanup:
        def __init__(self, _writer):
            self.before = snapshot
            self.cleanup = {
                "temporary_keychains_deleted": True,
                "user_keychain_metadata_unchanged": True,
            }
            self.directory = SimpleNamespace(name=str(tmp_path))
            self.keychains = [1]
            self.api = SimpleNamespace(call=lambda *args: {"data": probe.ORIGINAL})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            raise RuntimeError("synthetic cleanup failure")

        def add(self, **_kwargs):
            return b"synthetic-reference"

        def paths(self):
            return [tmp_path / "fixture-0.keychain-db"]

    def timeout(*_args, **_kwargs):
        if interrupted is not None:
            raise interrupted()
        raise subprocess.TimeoutExpired(
            ["synthetic-writer"],
            5,
            stderr=b"stage:before_update\narbitrary diagnostic must not escape\n",
        )

    monkeypatch.setattr(probe, "preflight", lambda: None)
    monkeypatch.setattr(probe, "Fixture", BrokenCleanup)
    monkeypatch.setattr(probe, "validate_scope", lambda _root, paths: paths)
    monkeypatch.setattr(probe, "capture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(probe, "environment_snapshot", lambda: snapshot)
    monkeypatch.setattr(probe.subprocess, "run", timeout)

    if interrupted is not None:
        with pytest.raises(interrupted):
            probe.run_case(tmp_path / "writer", "metadata-control")
        return

    result = probe.run_case(tmp_path / "writer", "metadata-control")

    assert result["failure"] == "timed_out"
    assert result["stages"] == ["before_update"]
    assert "arbitrary diagnostic" not in str(result)


def test_active_security_requester_blocks_before_metadata_or_fixture(monkeypatch):
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout=b"/usr/bin/security\n", stderr=b""
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("must stop before Keychain metadata or fixture creation")

    monkeypatch.setattr(probe, "environment_snapshot", forbidden)
    monkeypatch.setattr(probe, "Fixture", forbidden)

    report = probe.run()

    assert report["blocked_reason"] == "security_requester_active"
    assert report["cases"] == []
    assert report["matrix_complete"] is False


@pytest.mark.parametrize("unavailable", [False, True])
def test_locked_or_unavailable_metadata_blocks_fixture_creation(monkeypatch, unavailable):
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout=b"/usr/bin/other\n", stderr=b""
        ),
    )

    def snapshot():
        if unavailable:
            raise RuntimeError("arbitrary metadata diagnostic")
        return {"default": {"unlocked": False}}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("must not create a fixture")

    monkeypatch.setattr(probe, "environment_snapshot", snapshot)
    monkeypatch.setattr(probe, "Fixture", forbidden)

    result = probe.run_case(None, "metadata-control")

    assert result["preflight_blocked"] is True
    assert result["failure"] == ("metadata_unavailable" if unavailable else "login_keychain_locked")
    assert "arbitrary metadata diagnostic" not in str(result)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"CI": "true"},
        {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "RUNNER_ENVIRONMENT": "self-hosted",
            "RUNNER_OS": "macOS",
        },
    ],
)
def test_probe_quarantined_outside_explicit_ephemeral_ci(monkeypatch, changes):
    for name in (
        "CI",
        "GITHUB_ACTIONS",
        "RUNNER_ENVIRONMENT",
        "RUNNER_OS",
        "RUNWAY_NATIVE_SYNTHETIC",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in changes.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        probe.subprocess, "run", lambda *a, **k: pytest.fail("no subprocess outside CI")
    )
    monkeypatch.setattr(
        probe, "environment_snapshot", lambda: pytest.fail("no native metadata outside CI")
    )
    assert probe.run()["blocked_reason"] == "ephemeral_macos_ci_required"


@pytest.mark.parametrize(
    "report, expected",
    [
        ({"matrix_complete": False}, 1),
        ({"matrix_complete": True, "cases": [{"isolation_failed": True}]}, 1),
        ({"matrix_complete": True, "cases": [{}]}, 0),
    ],
)
def test_incomplete_or_unsafe_report_fails_process(report, expected):
    assert probe.report_exit_code(report) == expected
