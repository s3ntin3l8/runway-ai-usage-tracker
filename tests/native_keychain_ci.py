"""Hosted-only process-group deadline for the quarantined synthetic probe."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.native_keychain_public_update_probe import hosted_ci, report_exit_code, save_report


def supervise(path: Path) -> int:
    report = {
        "scope": "private-synthetic-keychains-public-api-only",
        "real_claude_credential_operations": 0,
        "claude_oauth_exchanges": 0,
        "cases": [],
        "matrix_complete": False,
        "candidate_found": False,
    }
    if not hosted_ci():
        report["blocked_reason"] = "ephemeral_macos_ci_required"
        save_report(path, report)
        return 1
    save_report(path, report)
    child, failure = None, None
    child_exit_code, cleanup_failed = None, False
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/native_keychain_public_update_probe.py"),
                "--isolated-synthetic-only",
                "--report",
                str(path),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_exit_code = child.wait(timeout=90)
    except subprocess.TimeoutExpired:
        failure = "process_group_timed_out"
    except (KeyboardInterrupt, SystemExit):
        failure = "interrupted"
    except OSError:
        failure = "probe_start_failed"
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                # The group has already exited; there are no remaining members to kill.
                pass
            except OSError:
                cleanup_failed = True
                failure = failure or "process_group_cleanup_failed"
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cleanup_failed = True
                failure = failure or "process_group_cleanup_failed"
    try:
        report = json.loads(path.read_text())
        if not isinstance(report, dict):
            raise ValueError("invalid report")
    except (OSError, ValueError):
        report = {"cases": []}
        failure = failure or "report_unavailable"
    if child_exit_code is not None:
        # A child gate failure is distinct from a supervisor failure. Keep its
        # blocked_reason/case diagnostics and record the actual process result.
        report["child_exit_code"] = child_exit_code
        if child_exit_code != 0:
            report.update(matrix_complete=False, candidate_found=False)
    if cleanup_failed:
        report["supervisor_cleanup_failed"] = True
    if failure:
        report.update(supervisor_failure=failure, matrix_complete=False, candidate_found=False)
    save_report(path, report)
    return int(bool(failure) or bool(child_exit_code) or report_exit_code(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    sys.exit(supervise(parser.parse_args().report))
