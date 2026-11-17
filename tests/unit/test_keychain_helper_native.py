"""Exercise native success and race checks using linked stubs, never a real Keychain."""

from __future__ import annotations

import os
import plistlib
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.sidecar_pkg.keychain_protocol import encode_frame

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin C harness")
ROOT = Path(__file__).resolve().parents[2]
ITEM = {
    "service": "Chrome Safe Storage",
    "account": "synthetic-account",
    "persistent": b"ref",
}


def frame(query):
    return encode_frame({"operation": "copy", "query": query, "values": {}})


def decode(data):
    result = []
    while data:
        assert len(data) >= 4
        length = struct.unpack(">I", data[:4])[0]
        assert len(data) >= 4 + length
        result.append(plistlib.loads(data[4 : 4 + length]))
        data = data[4 + length :]
    return result


@pytest.fixture(scope="module")
def native_harness(tmp_path_factory):
    binary = tmp_path_factory.mktemp("native-keychain-stub") / "kc_helper"
    result = subprocess.run(
        [
            "xcrun",
            "clang",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-deprecated-declarations",
            "-DSecItemCopyMatching=RunwayTestCopy",
            "-DSecKeychainSetUserInteractionAllowed=RunwayTestInteraction",
            "-framework",
            "CoreFoundation",
            "-framework",
            "Security",
            str(ROOT / "sidecar_app/keychain_helper/main.c"),
            str(ROOT / "tests/fixtures/keychain_helper/native_stubs.c"),
            "-o",
            str(binary),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    return binary


def run(
    native_harness,
    tmp_path,
    scenario="success",
    *,
    interactive=False,
    unknown=False,
    service="Chrome Safe Storage",
):
    log = tmp_path / "calls"
    requests = (
        frame({"persistent": b"never-issued"})
        if unknown
        else (frame({"service": service}) + frame({"persistent": b"ref"}))
    )
    process = subprocess.run(
        [str(native_harness)] + (["--interactive"] if interactive else []),
        input=requests,
        capture_output=True,
        check=False,
        timeout=5,
        env={
            **os.environ,
            "RUNWAY_TEST_NATIVE_LOG": str(log),
            "RUNWAY_TEST_NATIVE_SCENARIO": scenario,
        },
    )
    assert process.returncode == 0
    assert process.stderr == b""
    return decode(process.stdout), log.read_text().splitlines()


@pytest.mark.parametrize("interactive", [False, True])
def test_search_then_same_process_read(native_harness, tmp_path, interactive):
    responses, calls = run(native_harness, tmp_path, interactive=interactive)
    assert responses == [{"result": [ITEM]}, {"result": {**ITEM, "data": b"synthetic-secret"}}]
    assert calls == ["interaction:off", "search", "metadata"] + (
        ["interaction:on"] if interactive else []
    ) + ["data", "interaction:off"]


@pytest.mark.parametrize("interactive, maximum", [(False, 30), (True, 60)])
def test_native_read_deadline(native_harness, tmp_path, interactive, maximum):
    responses, calls = run(native_harness, tmp_path, "deadline", interactive=interactive)
    assert responses[-1]["result"]["data"] == b"synthetic-secret"
    remaining = int(next(call for call in calls if call.startswith("deadline:")).split(":")[1])
    # Inspect the live OS timer at the Security call without waiting for a prompt.
    assert maximum - 5 < remaining <= maximum


@pytest.mark.parametrize(
    "scenario", ["search-denied", "metadata-denied", "data-denied", "data-interaction-denied"]
)
def test_native_denials_never_return_data(native_harness, tmp_path, scenario):
    responses, calls = run(native_harness, tmp_path, scenario)
    if scenario == "search-denied":
        assert responses == [{"error": "denied"}, {"error": "unavailable"}]
        assert calls == ["interaction:off", "search"]
    else:
        assert responses == [{"result": [ITEM]}, {"error": "denied"}]
        assert calls[:3] == ["interaction:off", "search", "metadata"]
        assert ("data" in calls) == scenario.startswith("data-")


@pytest.mark.parametrize(
    "scenario", ["metadata-service", "metadata-account", "data-service", "data-account"]
)
def test_changed_identity_never_returns_secret(native_harness, tmp_path, scenario):
    responses, calls = run(native_harness, tmp_path, scenario)
    assert responses == [{"result": [ITEM]}, {"error": "unavailable"}]
    assert ("data" in calls) == scenario.startswith("data-")


def test_raw_reference_never_calls_security_copy(native_harness, tmp_path):
    responses, calls = run(native_harness, tmp_path, unknown=True)
    assert responses == [{"error": "unavailable"}]
    assert calls == ["interaction:off"]


def test_failed_partial_search_issues_no_references(native_harness, tmp_path):
    responses, calls = run(native_harness, tmp_path, "partial-search")
    assert responses == [{"error": "unavailable"}, {"error": "unavailable"}]
    assert calls == ["interaction:off", "search"]


@pytest.mark.parametrize(
    "service",
    [
        "Claude Code-credentials",
        "Claude Code-credentials-0123abcd",
        "Claude Code-credentials-ffffffff",
    ],
)
def test_strict_claude_family_success(native_harness, tmp_path, service):
    import pwd

    responses, calls = run(native_harness, tmp_path, "suffix", service=service)
    item = {**ITEM, "service": service, "account": pwd.getpwuid(os.getuid()).pw_name}
    assert responses == [{"result": [item]}, {"result": {**item, "data": b"synthetic-secret"}}]
    assert calls == ["interaction:off", "search", "metadata", "data", "interaction:off"]


@pytest.mark.parametrize(
    "service",
    [
        "Claude Code-credentials-",
        "Claude Code-credentials-1234567",
        "Claude Code-credentials-123456789",
        "Claude Code-credentials-1234ABCD",
        "Claude Code-credentials-1234567g",
        "Claude Code-credentials-1234567é",
        "Claude Code-credentials-1234567\0",
        "Claude Code-credentials\0",
        "Chrome Safe Storage\0",
    ],
)
def test_invalid_service_names_never_call_security(native_harness, tmp_path, service):
    responses, calls = run(native_harness, tmp_path, service=service)
    assert responses == [{"error": "unavailable"}, {"error": "unavailable"}]
    assert calls == ["interaction:off"]
