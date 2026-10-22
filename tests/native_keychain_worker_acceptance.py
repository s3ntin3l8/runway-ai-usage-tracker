"""Bounded packaged native boundary checks using only disposable synthetic Keychains."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.sidecar_pkg.keychain_client import KeychainClient
from scripts.sidecar_pkg.keychain_protocol import KeychainError
from tests.keychain_policy_fixture import capture, compare
from tests.native_claude_keychain_acceptance import ACCOUNT, DEFAULT, Fixture


def run(package: Path) -> dict:
    report = {
        "scope": "packaged-private-keychains-only",
        "real_credential_operations": 0,
        "real_oauth_exchanges": 0,
        "cases": [],
    }

    def client(timeout=3):
        result = KeychainClient(command=[str(package), "--runway-keychain-worker"], timeout=timeout)
        result.starts = 0
        original = result._start

        def start():
            result.starts += 1
            return original()

        result._start = start
        return result

    def query(fixture, **values):
        return dict(values, keychains=[str(path) for path in fixture.paths()])

    def error(call, expected):
        try:
            call()
        except KeychainError as exc:
            assert exc.reason in expected
            return exc.reason
        raise AssertionError("expected private helper rejection")

    fixture = Fixture(package)
    with fixture:
        ref = fixture.add()
        original = fixture.api.call("copy", query(fixture, persistent=ref), {})
        before = capture(DEFAULT, ACCOUNT, keychain=fixture.keychains[0])
        worker = client()
        try:
            assert (
                worker.call(
                    "check_update",
                    query(fixture, persistent=ref),
                    {},
                    group="claude-code",
                    allowed=lambda: True,
                )
                is False
            )
            error(
                lambda: worker.call(
                    "update",
                    query(fixture, persistent=ref),
                    {"data": b"synthetic-replacement"},
                    group="claude-code",
                    allowed=lambda: True,
                ),
                {"policy unavailable"},
            )
            assert fixture.api.call("copy", query(fixture, persistent=ref), {}) == original
            assert compare(before, capture(DEFAULT, ACCOUNT, keychain=fixture.keychains[0]))[
                "access_policy_preserved"
            ]
            report["cases"].append(
                {"case": "foreign_partition_rejected_before_write", "outcome": "passed"}
            )
        finally:
            worker.close()

        worker = client()
        try:
            reason = error(
                lambda: worker.call(
                    "copy", query(fixture, persistent=ref), {}, group="claude-code"
                ),
                {"denied"},
            )
            assert worker.starts == 1 and worker._process is None
            worker.close()
            worker.reopen()
            for operation in ("copy", "update", "check_update"):
                error(
                    lambda op=operation: worker.call(
                        op,
                        query(fixture, persistent=ref),
                        {},
                        group="claude-code",
                        allowed=lambda: True,
                    ),
                    {reason},
                )
            assert worker.starts == 1
            report["cases"].append(
                {
                    "case": "denial_suppresses_discovery_and_renewal_after_reopen",
                    "outcome": "passed",
                    "native_dispatches": 1,
                }
            )
        finally:
            worker.close()

        worker = client(timeout=1)
        process = None
        try:
            worker.call(
                "copy", query(fixture, service=DEFAULT, account=ACCOUNT), {}, group="claude-code"
            )
            process = worker._process
            assert process is not None and process.poll() is None
            os.kill(process.pid, signal.SIGSTOP)
            error(
                lambda: worker.call(
                    "copy",
                    query(fixture, service=DEFAULT, account=ACCOUNT),
                    {},
                    group="claude-code",
                ),
                {"timed_out"},
            )
            assert process.poll() is not None and worker._process is None
            starts = worker.starts
            error(
                lambda: worker.call(
                    "check_update",
                    query(fixture, persistent=ref),
                    {},
                    group="claude-code",
                    allowed=lambda: True,
                ),
                {"timed_out"},
            )
            assert worker.starts == starts
            report["cases"].append(
                {
                    "case": "timeout_reaps_suspended_packaged_helper_and_suppresses_retry",
                    "outcome": "passed",
                }
            )
        finally:
            if process is not None and process.poll() is None:
                process.kill()
            worker.close()

        foreign = Fixture(package)
        with foreign:
            other = foreign.add()
            other_before = foreign.api.call("copy", query(foreign, persistent=other), {})
            for operation in ("copy", "update", "delete", "check_update"):
                worker = client()
                try:
                    error(
                        lambda: worker.call(
                            operation,
                            query(fixture, persistent=other),
                            {"data": b"synthetic"} if operation == "update" else {},
                            group="claude-code",
                            allowed=lambda: True,
                        ),
                        {"unavailable", "missing"},
                    )
                finally:
                    worker.close()
            assert foreign.api.call("copy", query(foreign, persistent=other), {}) == other_before
            report["cases"].append(
                {
                    "case": "foreign_keychain_reference_rejected_for_every_operation",
                    "outcome": "passed",
                }
            )
        assert all(foreign.cleanup.values())
    assert all(fixture.cleanup.values())
    report["user_keychain_metadata_unchanged"] = True
    report["temporary_keychains_deleted"] = True
    report["outcome"] = "passed"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("requires native macOS")
    output = run(args.package.resolve())
    args.report.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output))
