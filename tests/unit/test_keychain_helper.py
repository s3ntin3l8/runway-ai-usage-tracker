"""Native helper contract tests; every request is rejected before Keychain access."""

from __future__ import annotations

import plistlib
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.sidecar_pkg.keychain_protocol import MAX_FRAME, OPERATIONS, TIMEOUT, encode_frame
from scripts.sidecar_pkg.source_catalog import catalog

ROOT = Path(__file__).resolve().parents[2]
HELPER_ROOT = ROOT / "sidecar_app" / "keychain_helper"
HELPER = HELPER_ROOT / "bin" / "kc_helper"
FIXTURES = ROOT / "tests" / "fixtures" / "keychain_helper"
FORBIDDEN_SERVICE = "runway-test-forbidden-service"
UNAVAILABLE = {"error": "unavailable"}
NATIVE = pytest.mark.skipif(sys.platform != "darwin", reason="macOS native helper")


def request(**changes: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "operation": "copy",
        "query": {"service": FORBIDDEN_SERVICE},
        "values": {},
    }
    result.update(changes)
    return result


def decode_frames(data: bytes) -> list[dict[str, Any]]:
    frames = []
    while data:
        assert len(data) >= 4
        length = struct.unpack(">I", data[:4])[0]
        assert 0 < length <= MAX_FRAME
        assert len(data) >= 4 + length
        frames.append(plistlib.loads(data[4 : 4 + length]))
        data = data[4 + length :]
    return frames


def run_helper(data: bytes, *, interactive: bool = False) -> subprocess.CompletedProcess[bytes]:
    assert HELPER.is_file(), "build the native helper before running Darwin contract tests"
    command = [str(HELPER)] + (["--interactive"] if interactive else [])
    result = subprocess.run(command, input=data, capture_output=True, timeout=5, check=False)
    assert result.stderr == b""
    return result


def test_native_service_allowlist_matches_catalog():
    source = (HELPER_ROOT / "main.c").read_text()
    match = re.search(r"const\s+char\s*\*\s*SERVICES\[\]\s*=\s*\{(.*?)\};", source, re.S)
    assert match is not None, "native service allowlist must remain explicitly auditable"
    services = re.findall(r'"([^"\\]*)"', match[1])
    assert len(services) == len(set(services))
    assert set(services) == {
        service for descriptor in catalog().values() for service in descriptor.services
    }


def test_native_frame_bounds_match_shared_protocol():
    source = (HELPER_ROOT / "main.c").read_text()
    for name, expected in (("MAX_FRAME", MAX_FRAME), ("TIMEOUT", TIMEOUT)):
        match = re.search(r"^#define " + name + r" (\d+)$", source, re.M)
        assert match is not None
        assert int(match[1]) == expected
    assert "copy" in OPERATIONS


def test_golden_frames_match_python_encoder():
    assert (FIXTURES / "forbidden-service.request.frame").read_bytes() == encode_frame(request())
    assert (FIXTURES / "unavailable.response.frame").read_bytes() == encode_frame(UNAVAILABLE)


@NATIVE
@pytest.mark.parametrize("interactive", [False, True])
def test_golden_native_roundtrip(interactive):
    result = run_helper(
        (FIXTURES / "forbidden-service.request.frame").read_bytes(), interactive=interactive
    )
    assert result.returncode == 0
    expected = decode_frames((FIXTURES / "unavailable.response.frame").read_bytes())
    assert decode_frames(result.stdout) == expected


@NATIVE
@pytest.mark.parametrize(
    "payload",
    [
        request(query={"persistent": b"synthetic-reference-never-issued"}),
        request(query={"service": FORBIDDEN_SERVICE, "account": "synthetic"}),
        request(query={"service": FORBIDDEN_SERVICE, "extra": "synthetic"}),
        request(query={"service": FORBIDDEN_SERVICE, "account": 123}),
        request(query={"service": FORBIDDEN_SERVICE, "persistent": b"synthetic"}),
        request(query={}),
        request(query=[]),
        request(values={"data": b"synthetic"}),
        request(extra="synthetic"),
        request(operation="unsupported"),
        *[
            request(operation=operation)
            for operation in ("check_update", "update", "add", "delete")
        ],
    ],
)
def test_invalid_requests_return_only_fixed_error(payload):
    result = run_helper(encode_frame(payload))
    assert result.returncode == 0
    assert decode_frames(result.stdout) == [UNAVAILABLE]


@NATIVE
def test_multiple_refusals_keep_framing_synchronized():
    frame = encode_frame(request())
    result = run_helper(frame * 3)
    assert result.returncode == 0
    assert decode_frames(result.stdout) == [UNAVAILABLE] * 3


@NATIVE
@pytest.mark.parametrize(
    "data",
    [
        b"\0\0",
        struct.pack(">I", MAX_FRAME + 1),
        struct.pack(">I", 0),
        struct.pack(">I", 16) + b"truncated",
        struct.pack(">I", 7) + b"invalid",
    ],
)
def test_invalid_frames_never_echo_input(data):
    result = run_helper(data)
    assert result.stdout == b""


@NATIVE
def test_clean_eof_exits_without_a_response():
    result = run_helper(b"")
    assert result.returncode == 0
    assert result.stdout == b""


@NATIVE
def test_unsupported_accounts_are_refused_before_keychain_access():
    import os
    import pwd

    forbidden_account = pwd.getpwuid(os.getuid()).pw_name + "-runway-forbidden"
    frames = b"".join(
        encode_frame(request(query={"service": service, "account": account}))
        for service, account in [
            ("Claude Code-credentials", forbidden_account),
            ("gh:github.com", forbidden_account),
            ("gemini", "runway-forbidden-account"),
            ("Chrome Safe Storage", "runway-forbidden-account"),
        ]
    )
    result = run_helper(frames)
    assert result.returncode == 0
    assert decode_frames(result.stdout) == [UNAVAILABLE] * 4


@NATIVE
@pytest.mark.parametrize(
    "service",
    [
        "Claude Code-credentials-1234567",
        "Claude Code-credentials-123456789",
        "Claude Code-credentials-1234ABCD",
        "Claude Code-credentials-1234567é",
        "Claude Code-credentials-1234567\0",
        "Claude Code-credentials\0",
        "Chrome Safe Storage\0",
    ],
)
def test_invalid_service_names_return_fixed_error(service):
    result = run_helper(encode_frame(request(query={"service": service})))
    assert result.returncode == 0
    assert decode_frames(result.stdout) == [UNAVAILABLE]
