"""No native frameworks: regressions for failed #773 probe cleanup/reporting."""

import json
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
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


def test_active_security_cli_blocks_before_metadata_or_fixture(monkeypatch):
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

    assert report["blocked_reason"] == "security_cli_active"
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


@pytest.fixture
def synthetic_case(monkeypatch, tmp_path):
    snapshot = {"default": {"path": "synthetic", "unlocked": True}, "search_list": []}

    class SyntheticFixture:
        def __init__(self, _writer):
            self.before = snapshot
            self.cleanup = {}
            self.directory = SimpleNamespace(name=str(tmp_path))
            self.keychains = [1]
            self.api = SimpleNamespace(call=lambda *args: {"data": probe.ORIGINAL})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.cleanup = {
                "temporary_keychains_deleted": True,
                "user_keychain_metadata_unchanged": True,
            }

        def add(self, **_kwargs):
            return b"synthetic-reference"

        def paths(self):
            return [tmp_path / "fixture-0.keychain-db"]

    monkeypatch.setattr(probe, "preflight", lambda: None)
    monkeypatch.setattr(probe, "Fixture", SyntheticFixture)
    monkeypatch.setattr(probe, "validate_scope", lambda _root, paths: paths)
    monkeypatch.setattr(probe, "capture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(probe, "environment_snapshot", lambda: snapshot)


@pytest.mark.parametrize("exit_code", [64, 65, 66])
def test_writer_failure_retains_only_allowlisted_stages(
    monkeypatch, tmp_path, synthetic_case, exit_code
):
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            exit_code,
            stdout=b"private-output",
            stderr=b"stage:start\nstage:private-detail\nprivate-stderr\nstage:before_item_metadata\n",
        ),
    )
    result = probe.run_case(tmp_path / "writer", "metadata-control")
    assert result["stages"] == ["start", "before_item_metadata"]
    assert result["writer_exit_code"] == exit_code
    assert "failure" in result
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("phase,index", [("compile", 0), ("sign", 1), ("verify_signature", 2)])
@pytest.mark.parametrize(
    "failure", [OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired]
)
def test_build_failures_save_sanitized_nonzero_report(monkeypatch, tmp_path, phase, index, failure):
    monkeypatch.setattr(probe, "preflight", lambda: None)
    calls = []

    def execute(command, **_kwargs):
        calls.append(command)
        if len(calls) - 1 == index:
            if failure is OSError:
                raise OSError("private-build-detail")
            if failure is subprocess.CalledProcessError:
                raise failure(1, command, output=b"private-build-detail", stderr=b"private-error")
            raise failure(command, 1, stderr=b"private-build-detail")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(probe.subprocess, "run", execute)
    path = tmp_path / "report.json"
    code = probe.main(SimpleNamespace(case=None, report=path))
    report = json.loads(path.read_text())
    assert code != 0
    assert report["failure"] == phase + "_failed"
    assert report["matrix_complete"] is False
    assert report["candidate_found"] is False
    assert report["cases"] == []
    assert "private-build-detail" not in path.read_text()
    assert "private-error" not in path.read_text()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("phase", ["build", "case"])
def test_interruptions_save_report_and_exit_nonzero(monkeypatch, tmp_path, interruption, phase):
    monkeypatch.setattr(probe, "preflight", lambda: None)

    def interrupt(*_args, **_kwargs):
        raise interruption("private-interruption-detail")

    monkeypatch.setattr(
        probe.subprocess, "run", interrupt if phase == "build" else lambda *a, **k: None
    )
    monkeypatch.setattr(probe, "run_case", interrupt)
    path = tmp_path / "report.json"
    assert probe.main(SimpleNamespace(case=None, report=path)) != 0
    report = json.loads(path.read_text())
    assert report["failure"] == "interrupted"
    assert report["matrix_complete"] is False
    assert report["candidate_found"] is False
    assert "private-interruption-detail" not in path.read_text()


def test_security_daemon_does_not_block_cli_preflight(monkeypatch):
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [],
            0,
            stdout=b"/usr/sbin/securityd\n/usr/libexec/securityd_service\n",
        ),
    )
    monkeypatch.setattr(probe, "environment_snapshot", lambda: {"default": {"unlocked": True}})
    assert probe.preflight() is None


def test_native_synthetic_lookup_matches_python_fixture_identity():
    source = (Path(probe.ROOT) / "tests/native/keychain_public_update_probe.c").read_text()
    assert re.findall(r'kSecAttrService, CFSTR\("([^"\n]+)"\)', source) == [probe.SERVICE]
    assert re.findall(r'kSecAttrAccount, CFSTR\("([^"\n]+)"\)', source) == [probe.ACCOUNT]
    assert re.findall(r'const unsigned char original\[\] = "([^"\n]+)"', source) == [
        probe.ORIGINAL.decode()
    ]


def test_successful_writer_cannot_export_unexpected_diagnostics(
    monkeypatch, tmp_path, synthetic_case
):
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [],
            0,
            stdout=b'{"update_status":0,"read_status":0,"replacement_verified":false,"unexpected":"private-detail"}',
            stderr=b"stage:after_update\nprivate-detail\n",
        ),
    )
    monkeypatch.setattr(probe, "compare", lambda *a: {"access_policy_preserved": True})
    result = probe.run_case(tmp_path / "writer", "metadata-control")
    assert "failure" not in result
    assert result["stages"] == ["after_update"]
    assert "private-detail" not in json.dumps(result)
    assert "unexpected" not in result


def test_post_cleanup_metadata_change_is_failed_isolation(monkeypatch, tmp_path, synthetic_case):
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [],
            0,
            stdout=b'{"update_status":0,"read_status":0,"replacement_verified":false}',
            stderr=b"",
        ),
    )
    monkeypatch.setattr(probe, "compare", lambda *a: {"access_policy_preserved": True})
    monkeypatch.setattr(
        probe,
        "environment_snapshot",
        lambda: {
            "default": {"path": "synthetic", "unlocked": False},
            "search_list": [],
        },
    )
    result = probe.run_case(tmp_path / "writer", "metadata-control")
    assert result["metadata_checks"]["default_lock_same"] is False
    assert result["isolation_failed"] is True
    assert probe.report_exit_code({"matrix_complete": True, "cases": [result]}) == 1


@pytest.mark.parametrize("incomplete", [False, True])
def test_only_complete_safe_matrix_can_report_candidate(monkeypatch, tmp_path, incomplete):
    monkeypatch.setattr(probe, "preflight", lambda: None)
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: None)

    def case(_writer, mode):
        if incomplete and mode == "data-only":
            raise OSError("private-run-detail")
        return {"case": mode, "candidate_preserves_shared_access": True}

    monkeypatch.setattr(probe, "run_case", case)
    path = tmp_path / "report.json"
    result = probe.run(("metadata-control", "data-only"), report_path=path)
    assert probe.report_exit_code(result) == int(incomplete)
    assert result["candidate_found"] is (not incomplete)
    assert result["matrix_complete"] is (not incomplete)
    assert "private-run-detail" not in path.read_text()


@pytest.fixture
def saved_case(monkeypatch, tmp_path, synthetic_case):
    def run(stdout=b"{}", *, timeout=False):
        def execute(command, **_kwargs):
            if Path(command[0]).name == "synthetic-writer":
                if timeout:
                    raise subprocess.TimeoutExpired(command, 5, stderr=b"stage:before_update\n")
                return subprocess.CompletedProcess(
                    command, 0, stdout=stdout, stderr=b"stage:after_update\nprivate-detail\n"
                )
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(probe.subprocess, "run", execute)
        path = tmp_path / "saved.json"
        probe.run(("metadata-control",), report_path=path)
        result = json.loads(path.read_text())
        assert len(result["cases"]) == 1
        assert probe.report_exit_code(result) == 1
        assert result["candidate_found"] is False
        assert "private-detail" not in path.read_text()
        return result["cases"][0]

    return run


@pytest.mark.parametrize(
    "stdout",
    [
        b"null",
        b'"private-detail"',
        b"[]",
        b"{}",
        b'{"update_status":0}',
        b'{"update_status":0,"read_status":0,"replacement_verified":1}',
        b"invalid-json",
    ],
)
def test_malformed_writer_result_retains_saved_case_and_stages(saved_case, stdout):
    row = saved_case(stdout)
    assert row["failure"] == "invalid_writer_result"
    assert row["stages"] == ["after_update"]
    assert row["writer_exit_code"] == 0


@pytest.mark.parametrize("snapshot_error", [OSError, RuntimeError, subprocess.TimeoutExpired])
def test_failed_post_cleanup_snapshot_retains_primary_timeout(
    monkeypatch, saved_case, snapshot_error
):
    def unavailable():
        if snapshot_error is subprocess.TimeoutExpired:
            raise snapshot_error(["private-detail"], 5)
        raise snapshot_error("private-detail")

    monkeypatch.setattr(probe, "environment_snapshot", unavailable)
    row = saved_case(timeout=True)
    assert row["failure"] == "timed_out"
    assert row["stages"] == ["before_update"]
    assert row["metadata_failure"] == "metadata_unavailable"
    assert row["isolation_failed"] is True


def test_extra_failed_snapshot_comparison_is_not_ignored(monkeypatch, tmp_path, synthetic_case):
    monkeypatch.setattr(
        probe,
        "snapshot_checks",
        lambda *args: {
            "default_same": True,
            "default_lock_same": True,
            "search_list_same": True,
            "search_locks_same": True,
            "future_check": False,
        },
    )
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [],
            66,
            stdout=b"",
            stderr=b"stage:start\n",
        ),
    )
    row = probe.run_case(tmp_path / "writer", "metadata-control")
    assert row["isolation_failed"] is True


@pytest.mark.parametrize("missing", ["before", "checks"])
def test_missing_snapshot_checks_are_unsafe(monkeypatch, tmp_path, synthetic_case, missing):
    if missing == "before":
        original = probe.Fixture

        class MissingBefore(original):
            def __init__(self, *args):
                super().__init__(*args)
                del self.before

        monkeypatch.setattr(probe, "Fixture", MissingBefore)
    else:
        monkeypatch.setattr(probe, "snapshot_checks", lambda *args: {})
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [],
            66,
            stdout=b"",
            stderr=b"stage:start\n",
        ),
    )
    row = probe.run_case(tmp_path / "writer", "metadata-control")
    assert row["stages"] == ["start"]
    assert row["writer_exit_code"] == 66
    assert row["isolation_failed"] is True


@pytest.mark.parametrize("case", ["read-only-control", "reference-control", "writer-read-control"])
def test_read_only_control_records_ordered_isolation_without_candidate(
    monkeypatch, tmp_path, synthetic_case, case
):
    monkeypatch.setattr(probe, "compare", lambda *args: {"access_policy_preserved": True})
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout=b'{"control_completed":true,"control_status":0}',
            stderr=b"stage:owner_verified\nstage:control_complete\n",
        ),
    )
    row = probe.run_case(tmp_path / "writer", case)
    assert "failure" not in row
    assert row["control_only"] is True
    assert row["update_attempted"] is False
    assert row["candidate_preserves_shared_access"] is False
    assert row["original_reader"] == "original"
    assert [entry["stage"] for entry in row["isolation_checkpoints"]] == [
        "fixture_created",
        "fixture_seeded",
        "fixture_read",
        "before_writer",
        "after_writer",
        "after_readback",
        "after_cleanup",
    ]
    assert all(all(entry["checks"].values()) for entry in row["isolation_checkpoints"])


@pytest.mark.parametrize("changed_at", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("case", ["read-only-control", "reference-control", "writer-read-control"])
def test_control_stops_at_first_isolation_change(
    monkeypatch, tmp_path, synthetic_case, changed_at, case
):
    snapshots, writers = [], []

    def snapshot():
        snapshots.append(True)
        return {
            "default": {"path": "synthetic", "unlocked": len(snapshots) < changed_at},
            "search_list": [],
        }

    def writer(*args, **kwargs):
        writers.append(True)
        return subprocess.CompletedProcess(
            [],
            0,
            stdout=b'{"control_completed":true,"control_status":0}',
            stderr=b"stage:control_complete\n",
        )

    monkeypatch.setattr(probe, "environment_snapshot", snapshot)
    monkeypatch.setattr(probe, "compare", lambda *args: {"access_policy_preserved": True})
    monkeypatch.setattr(probe.subprocess, "run", writer)
    row = probe.run_case(tmp_path / "writer", case)
    assert row["failure"] == "isolation_changed"
    assert row["isolation_failed"] is True
    assert row["candidate_preserves_shared_access"] is False
    assert len(writers) == (0 if changed_at <= 4 else 1)
    first_bad = next(
        entry for entry in row["isolation_checkpoints"] if not all(entry["checks"].values())
    )
    assert (
        first_bad["stage"]
        == [
            "fixture_created",
            "fixture_seeded",
            "fixture_read",
            "before_writer",
            "after_writer",
            "after_readback",
        ][changed_at - 1]
    )


def test_control_checkpoints_are_durable_before_native_child(monkeypatch, tmp_path, synthetic_case):
    path = tmp_path / "report.json"

    def execute(command, **kwargs):
        if Path(command[0]).name == "synthetic-writer":
            saved = json.loads(path.read_text())
            assert saved["selected_cases"] == ["read-only-control"]
            progress = saved["active_case_progress"]
            assert progress["isolation_checkpoints"][-1]["stage"] == "before_writer"
            assert progress["update_attempted"] is False
            assert progress["candidate_preserves_shared_access"] is False
            raise KeyboardInterrupt("private-interruption-detail")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(probe.subprocess, "run", execute)
    assert probe.main(SimpleNamespace(case="read-only-control", report=path)) == 130
    saved = json.loads(path.read_text())
    assert saved["failure"] == "interrupted"
    assert saved["candidate_found"] is False
    assert saved["active_case_progress"]["isolation_checkpoints"][-1]["stage"] == "before_writer"
    assert "private-interruption-detail" not in path.read_text()
    assert "synthetic-reference" not in path.read_text()
