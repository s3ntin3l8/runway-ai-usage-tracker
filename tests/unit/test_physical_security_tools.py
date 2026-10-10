"""Fixture correctness matters: a blocked browser must not be labeled app rejection."""

import json
import struct

import pytest

from scripts.security_browser_probe import dns_answer
from scripts.security_native_update_probe import installed_hashes, probe


def question(name="probe.example", kind=1):
    labels = b"".join(bytes([len(label)]) + label.encode() for label in name.split("."))
    return (
        b"\x12\x34"
        + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0)
        + labels
        + b"\0"
        + struct.pack("!HH", kind, 1)
    )


def test_rebinding_dns_switches_exact_controlled_host_with_zero_ttl():
    query = question()
    initial = dns_answer(query, "probe.example", "192.0.2.1")
    rebound = dns_answer(query, "probe.example", "127.0.0.1")
    assert initial[:2] == query[:2]
    assert initial[-4:] == b"\xc0\x00\x02\x01"
    assert rebound[-4:] == b"\x7f\x00\x00\x01"
    assert struct.unpack_from("!I", rebound, len(query) + 6)[0] == 0
    assert struct.unpack_from("!H", rebound, 6)[0] == 1


def test_dns_never_forwards_other_hosts_or_returns_unplanned_ipv6():
    assert (
        struct.unpack_from(
            "!H", dns_answer(question("other.example"), "probe.example", "127.0.0.1"), 2
        )[0]
        & 15
        == 3
    )
    assert (
        struct.unpack_from("!H", dns_answer(question(kind=28), "probe.example", "127.0.0.1"), 6)[0]
        == 0
    )


@pytest.mark.parametrize("query", [b"", b"\0" * 12, question()[:-1], question()[:13]])
def test_malformed_dns_questions_rejected(query):
    with pytest.raises(ValueError):
        dns_answer(query, "probe.example", "127.0.0.1")


def test_native_driver_records_refusals_and_preserves_install(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.security_native_update_probe.verify_update", lambda *args: None)
    installed = tmp_path / "installed"
    installed.write_bytes(b"existing executable")
    previous = tmp_path / "installed.previous"
    previous.write_bytes(b"existing rollback")
    archive = tmp_path / "payload.zip"
    archive.write_bytes(b"synthetic archive")
    bundle = tmp_path / "bundle.json"
    bundle.write_text("invalid bundle")
    output = tmp_path / "evidence.json"
    before = installed_hashes(installed)
    # This branch may precede #605; the cap case is honestly blocked then.
    probe(installed, archive, bundle, output)
    report = json.loads(output.read_text())
    assert installed_hashes(installed) == before
    assert previous.read_bytes() == b"existing rollback"
    assert [c["case"] for c in report["cases"]] == [
        "missing-bundle",
        "malformed-bundle",
        "tampered-payload",
        "oversized-response",
    ]
    assert all(c["status"] == "pass" for c in report["cases"][:3])
    assert all(c["stages"]["lock_acquired"] for c in report["cases"][:3])
    assert report["cases"][-1]["status"] in {"pass", "blocked"}


def test_native_driver_requires_a_valid_signed_positive_baseline(tmp_path, monkeypatch):
    from scripts.sidecar_pkg.signatures import UpdateVerificationError

    def reject(*args):
        raise UpdateVerificationError("invalid baseline")

    monkeypatch.setattr("scripts.security_native_update_probe.verify_update", reject)
    with pytest.raises(UpdateVerificationError, match="invalid baseline"):
        probe(tmp_path / "unused", tmp_path / "archive", tmp_path / "bundle", tmp_path / "evidence")
    assert not (tmp_path / "evidence").exists()


def test_uncapped_downloader_cannot_pass_oversize_probe(tmp_path, monkeypatch):
    from scripts.sidecar_pkg import self_update

    monkeypatch.setattr("scripts.security_native_update_probe.verify_update", lambda *args: None)
    # raising=False permits adding the constant before #605; it still replaces
    # an existing attribute after #605 (it never skips assignment).
    monkeypatch.setattr(self_update, "MAX_ARCHIVE_BYTES", 10, raising=False)
    assert self_update.MAX_ARCHIVE_BYTES == 10

    def uncapped(url, dest, **kwargs):
        from urllib.request import Request

        with self_update.request.urlopen(Request(url)) as response:
            dest.write_bytes(response.read())

    monkeypatch.setattr(self_update, "_download", uncapped)
    for name in ("installed", "payload.zip", "bundle"):
        (tmp_path / name).write_bytes(b"synthetic")
    output = tmp_path / "evidence.json"
    assert not probe(tmp_path / "installed", tmp_path / "payload.zip", tmp_path / "bundle", output)
    oversized = json.loads(output.read_text())["cases"][-1]
    assert oversized["status"] == "fail"
    assert oversized["stages"]["body_read"]
    assert not oversized["expected_refusal_stage"]


def test_probe_cannot_pass_without_real_lock_acquisition(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from scripts.sidecar_pkg import self_update

    @contextmanager
    def unlocked():
        yield True

    monkeypatch.setattr(self_update, "_single_flight", unlocked)
    monkeypatch.setattr("scripts.security_native_update_probe.verify_update", lambda *args: None)
    for name in ("installed", "payload.zip", "bundle"):
        (tmp_path / name).write_bytes(b"synthetic")
    output = tmp_path / "evidence.json"
    assert not probe(tmp_path / "installed", tmp_path / "payload.zip", tmp_path / "bundle", output)
    cases = json.loads(output.read_text())["cases"][:3]
    assert all(c["status"] == "fail" and not c["stages"]["lock_acquired"] for c in cases)
