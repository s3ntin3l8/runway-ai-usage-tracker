"""Explicit synthetic-only comparison of public Keychain update APIs for #773.

Not a pytest test or a shipping worker. Every case uses a fresh private Keychain,
fake data, and a separately signed native writer. No provider request is made.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.sidecar_pkg.keychain_protocol import KeychainError
from tests.keychain_policy_fixture import capture, compare
from tests.native_claude_keychain_acceptance import ACCOUNT, Fixture, environment_snapshot
from tests.native_keychain_isolation import validate_scope

SERVICE = "Runway synthetic public-update probe"
ORIGINAL = b"synthetic-public-update-original"
MODES = ("metadata-control", "data-only", "legacy-data", "combined-access")
STAGES = {
    "start",
    "interaction_disabled",
    "private_keychain_opened",
    "before_item_metadata",
    "owner_verified",
    "before_update",
    "after_update",
}


def stages(raw: bytes | None) -> list[str]:
    """Accept only fixed stage names, never arbitrary subprocess diagnostics."""
    return [
        line.removeprefix("stage:")
        for line in (raw or b"").decode(errors="replace").splitlines()
        if line.startswith("stage:") and line.removeprefix("stage:") in STAGES
    ]


def snapshot_checks(before: dict, after: dict) -> dict[str, bool]:
    """Report metadata differences without exposing user paths."""
    return {
        "default_same": before["default"]["path"] == after["default"]["path"],
        "default_lock_same": before["default"]["unlocked"] == after["default"]["unlocked"],
        "search_list_same": [row["path"] for row in before["search_list"]]
        == [row["path"] for row in after["search_list"]],
        "search_locks_same": before["search_list"] == after["search_list"],
    }


def hosted_ci() -> bool:
    """Pure environment gate, checked before subprocesses or native metadata."""
    return all(
        os.environ.get(name) == value
        for name, value in {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_OS": "macOS",
            "RUNWAY_NATIVE_SYNTHETIC": "1",
        }.items()
    )


def preflight() -> str | None:
    """Stop before fixture creation if the host cannot support a quiet probe."""
    if not hosted_ci():
        return "ephemeral_macos_ci_required"
    try:
        processes = subprocess.run(
            ["/bin/ps", "-axo", "comm="],
            capture_output=True,
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "requester_check_unavailable"
    if any(
        Path(line.strip()).name == "security"
        for line in processes.stdout.decode(errors="replace").splitlines()
    ):
        return "security_cli_active"
    try:
        snapshot = environment_snapshot()
        if snapshot["default"]["unlocked"] is not True:
            return "login_keychain_locked"
    except (OSError, RuntimeError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        return "metadata_unavailable"
    return None


def run_case(writer: Path, mode: str) -> dict:
    reason = preflight()
    if reason is not None:
        return {"case": mode, "failure": reason, "preflight_blocked": True}
    fixture = Fixture(writer)
    row = {"case": mode}
    interrupted = None
    try:
        with fixture:
            try:
                reference = fixture.add(service=SERVICE, data=ORIGINAL)
                paths = validate_scope(Path(fixture.directory.name), fixture.paths())
                assert len(paths) == 1
                query = {"persistent": reference, "keychains": [str(path) for path in paths]}
                assert fixture.api.call("copy", query, {})["data"] == ORIGINAL
                before = capture(SERVICE, ACCOUNT, keychain=fixture.keychains[0])
                try:
                    child = subprocess.run(
                        [str(writer), str(paths[0]), mode],
                        capture_output=True,
                        timeout=5,
                    )
                except subprocess.TimeoutExpired as exc:
                    row["failure"] = "timed_out"
                    row["stages"] = stages(exc.stderr)
                    raise
                row["stages"] = stages(child.stderr)
                row["writer_exit_code"] = child.returncode
                assert child.returncode == 0, "synthetic public writer failed"
                values = json.loads(child.stdout)
                for name, kind in (
                    ("update_status", int),
                    ("read_status", int),
                    ("replacement_verified", bool),
                ):
                    assert type(values[name]) is kind, "invalid writer result"
                    row[name] = values[name]
                row.update(
                    compare(before, capture(SERVICE, ACCOUNT, keychain=fixture.keychains[0]))
                )
                try:
                    unchanged = fixture.api.call("copy", query, {})["data"] == ORIGINAL
                    row["original_reader"] = "original" if unchanged else "replacement"
                except KeychainError as exc:
                    assert exc.reason == "denied"
                    row["original_reader"] = "denied"
                if mode == "metadata-control":
                    assert row["update_status"] == 0
                    assert row["access_policy_preserved"]
                    assert row["original_reader"] == "original"
                row["candidate_preserves_shared_access"] = (
                    mode != "metadata-control"
                    and row["update_status"] == 0
                    and row["replacement_verified"]
                    and row["access_policy_preserved"]
                    and row["original_reader"] == "replacement"
                )
            except (KeyboardInterrupt, SystemExit) as exc:
                interrupted = exc
                raise
    except (AssertionError, KeychainError, subprocess.SubprocessError, ValueError, RuntimeError):
        if interrupted is not None:
            raise interrupted from None
        # Preserve a timeout even when the fixture's cleanup assertion also fails.
        row.setdefault("failure", "native_probe_incomplete")
    row.update(fixture.cleanup)
    if hasattr(fixture, "before"):
        row["metadata_checks"] = snapshot_checks(fixture.before, environment_snapshot())
    if not all(
        fixture.cleanup.get(name) is True
        for name in ("temporary_keychains_deleted", "user_keychain_metadata_unchanged")
    ) or not all(row.get("metadata_checks", {}).values()):
        row["isolation_failed"] = True
    return row


def save_report(path: Path, report: dict) -> None:
    """Keep the last complete sanitized snapshot if the outer deadline kills us."""
    fd, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(report, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(selected: tuple[str, ...] = MODES, *, report_path: Path | None = None) -> dict:
    report = {
        "scope": "private-synthetic-keychains-public-api-only",
        "real_claude_credential_operations": 0,
        "claude_oauth_exchanges": 0,
        "cases": [],
        "matrix_complete": False,
        "candidate_found": False,
    }
    phase = "preflight"

    def checkpoint(value: str) -> None:
        nonlocal phase
        phase = value
        report["phase"] = value
        if report_path is not None:
            save_report(report_path, report)

    try:
        checkpoint("preflight")
        reason = preflight()
        if reason is not None:
            report["blocked_reason"] = reason
            return report
        with tempfile.TemporaryDirectory(prefix="runway-public-update-build-") as build:
            writer = Path(build) / "synthetic-writer"
            checkpoint("compile")
            subprocess.run(
                [
                    "/usr/bin/clang",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-Wno-deprecated-declarations",
                    str(ROOT / "tests/native/keychain_public_update_probe.c"),
                    "-framework",
                    "Security",
                    "-framework",
                    "CoreFoundation",
                    "-o",
                    str(writer),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            checkpoint("sign")
            subprocess.run(
                ["/usr/bin/codesign", "--force", "--sign", "-", str(writer)],
                check=True,
                capture_output=True,
                timeout=10,
            )
            checkpoint("verify_signature")
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--strict", str(writer)],
                check=True,
                capture_output=True,
                timeout=10,
            )
            for mode in selected:
                checkpoint("case")
                report["active_case"] = mode
                if report_path is not None:
                    save_report(report_path, report)
                row = run_case(writer, mode)
                report["cases"].append(row)
                if "failure" in row or row.get("isolation_failed"):
                    report["stopped_after_failure"] = True
                    break
        report["matrix_complete"] = (
            bool(selected)
            and len(report["cases"]) == len(selected)
            and not report.get("stopped_after_failure", False)
        )
        report["candidate_found"] = report["matrix_complete"] and any(
            row.get("candidate_preserves_shared_access", False) for row in report["cases"]
        )
    except (KeyboardInterrupt, SystemExit):
        report["failure"] = "interrupted"
        raise
    except Exception:
        # This experiment's diagnostic boundary deliberately excludes exception
        # text, command arguments, native stdout/stderr and user paths.
        report["failure"] = phase + "_failed"
    finally:
        if report_path is not None:
            save_report(report_path, report)
    return report


def report_exit_code(report: dict) -> int:
    """An incomplete experiment is a failed gate, never evidence of safety."""
    return int(
        report.get("matrix_complete") is not True
        or "failure" in report
        or any(row.get("isolation_failed") or "failure" in row for row in report.get("cases", []))
    )


def main(args) -> int:
    try:
        result = run((args.case,) if args.case else MODES, report_path=args.report)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        return 1
    print(json.dumps(result))
    return report_exit_code(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-synthetic-only", action="store_true", required=True)
    parser.add_argument("--case", choices=MODES)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("requires native macOS")
    sys.exit(main(args))
