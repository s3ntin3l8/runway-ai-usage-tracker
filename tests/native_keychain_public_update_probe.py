"""Explicit synthetic-only comparison of public Keychain update APIs for #773.

Not a pytest test or a shipping worker. Every case uses a fresh private Keychain,
fake data, and a separately signed native writer. No provider request is made.
"""

from __future__ import annotations

import argparse
import json
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


def run_case(writer: Path, mode: str) -> dict:
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
                assert child.returncode == 0, "synthetic public writer failed"
                row.update(json.loads(child.stdout))
                row["stages"] = stages(child.stderr)
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
    if not all(fixture.cleanup.values()):
        row["isolation_failed"] = True
    return row


def run(selected: tuple[str, ...] = MODES) -> dict:
    report = {
        "scope": "private-synthetic-keychains-public-api-only",
        "real_claude_credential_operations": 0,
        "claude_oauth_exchanges": 0,
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="runway-public-update-build-") as build:
        writer = Path(build) / "synthetic-writer"
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
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", str(writer)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(writer)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        for mode in selected:
            row = run_case(writer, mode)
            report["cases"].append(row)
            if "failure" in row or row.get("isolation_failed"):
                report["stopped_after_failure"] = True
                break
    report["candidate_found"] = any(
        row.get("candidate_preserves_shared_access", False) for row in report["cases"]
    )
    report["matrix_complete"] = len(report["cases"]) == len(selected) and not report.get(
        "stopped_after_failure", False
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-synthetic-only", action="store_true", required=True)
    parser.add_argument("--case", choices=MODES)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("requires native macOS")
    result = run((args.case,) if args.case else MODES)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
