"""Published acceptance keeps independent failures and captured bytes observable."""

import copy
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import audit_published_images as published
from scripts import capture_release_updates as capture
from scripts import security_packaging_smoke as packaging


def test_daily_audits_prepare_one_database_and_attempt_both(monkeypatch, tmp_path):
    prepared, audited = [], []

    def prepare(cache):
        prepared.append(cache)
        return {
            "built": datetime.now(UTC).isoformat(),
            "valid": True,
            "from": "https://example.com/?checksum=sha256:" + "a" * 64,
        }

    def audit(image, output, *, database):
        audited.append((image, output, database))
        if output.name == "edge":
            raise RuntimeError("edge scan failed")
        return False

    monkeypatch.setattr(published, "prepare_database", prepare)
    monkeypatch.setattr(published, "audit", audit)
    monkeypatch.setattr(
        published.subprocess, "check_output", lambda *a, **kw: json.dumps("sha256:" + "b" * 64)
    )
    assert published.audit_published("v3.0.0-beta.8", tmp_path)
    assert len(prepared) == 1
    assert [x[1].name for x in audited] == ["edge", "release"]
    assert all(x[2] == prepared[0] for x in audited)
    assert not prepared[0].exists()
    assert json.loads((tmp_path / "edge/evidence.json").read_text())["passed"] is False


@pytest.mark.parametrize("failure", ["preparation", "stale"])
def test_shared_database_failure_records_both_targets(monkeypatch, tmp_path, failure):
    def prepare(cache):
        if failure == "preparation":
            raise RuntimeError("database unavailable")
        return {
            "built": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
            "valid": True,
            "from": "https://example.com/?checksum=sha256:" + "a" * 64,
        }

    monkeypatch.setattr(published, "prepare_database", prepare)
    monkeypatch.setattr(published, "audit", lambda *a, **kw: pytest.fail("untrusted DB audited"))
    assert published.audit_published("v3.0.0-beta.8", tmp_path)
    for label in ("edge", "release"):
        assert json.loads((tmp_path / label / "evidence.json").read_text())["passed"] is False


def test_audit_failure_return_does_not_short_circuit_second_target(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        published.subprocess, "check_output", lambda *a, **kw: json.dumps("sha256:" + "b" * 64)
    )

    def audit(image, output, *, database):
        calls.append(output.name)
        return output.name == "edge"

    monkeypatch.setattr(published, "audit", audit)
    assert published.audit_targets("v3.0.0-beta.8", tmp_path, tmp_path)
    assert calls == ["edge", "release"]


@pytest.fixture
def release_capture(monkeypatch):
    tag = "v3.0.0-beta.8"
    blobs = {}
    for platform, extension in capture.PLATFORMS.items():
        name = f"Runway-Sidecar-{platform}-{tag}.{extension}"
        data = f"payload-{platform}".encode()
        blobs[name] = data
        blobs[name + ".sigstore.json"] = b'{"mediaType":"modern"}'
        blobs[name + ".sha256"] = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
    metadata = {
        "id": 42,
        "tag_name": tag,
        "draft": False,
        "assets": [
            {
                "id": i,
                "name": name,
                "size": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "updated_at": "2026-10-11",
            }
            for i, (name, data) in enumerate(blobs.items())
        ],
    }
    commits, verifies = ["a" * 40], []
    snapshots = [metadata]
    monkeypatch.setattr(
        capture,
        "api",
        lambda path: (
            copy.deepcopy(snapshots[-1])
            if path.startswith("releases")
            else {"object": {"type": "commit", "sha": commits[-1]}}
        ),
    )
    monkeypatch.setattr(
        capture, "download", lambda tag, name, output: output.write_bytes(blobs[name])
    )
    monkeypatch.setattr(
        capture, "verify_update", lambda payload, bundle: verifies.append(payload.name)
    )
    monkeypatch.setattr(capture.subprocess, "check_output", lambda *a, **kw: "b" * 40)
    return tag, blobs, metadata, commits, verifies, snapshots


def test_capture_complete_release_and_recheck_integrity(release_capture, tmp_path):
    tag, blobs, metadata, commits, verifies, snapshots = release_capture
    directory = tmp_path / "capture"
    manifest = capture.capture(tag, directory)
    assert manifest["passed"] and manifest["source_commit"] == "a" * 40
    assert len(verifies) == 4
    for platform in capture.PLATFORMS:
        payload, bundle = capture.captured_payload(directory, platform)
        assert payload.read_bytes() == blobs[payload.name]
        assert bundle.name == payload.name + ".sigstore.json"
    payload.write_bytes(b"replaced")
    with pytest.raises(ValueError, match="integrity"):
        capture.captured_payload(directory, "Windows")


@pytest.mark.parametrize("change", ["missing", "duplicate", "size", "digest", "checksum"])
def test_capture_refuses_incomplete_or_changed_assets(release_capture, tmp_path, change):
    tag, blobs, metadata, commits, verifies, snapshots = release_capture
    if change == "missing":
        metadata["assets"].pop()
    elif change == "duplicate":
        metadata["assets"].append(metadata["assets"][0])
    elif change == "size":
        metadata["assets"][0]["size"] += 1
    elif change == "digest":
        metadata["assets"][0]["digest"] = "sha256:" + "0" * 64
    else:
        name = next(n for n in blobs if n.endswith(".sha256"))
        blobs[name] = b"incorrect checksum"
        entry = next(a for a in metadata["assets"] if a["name"] == name)
        entry.update(
            size=len(blobs[name]), digest="sha256:" + hashlib.sha256(blobs[name]).hexdigest()
        )
    with pytest.raises(ValueError):
        capture.capture(tag, tmp_path / "capture")


@pytest.mark.parametrize("change", ["release", "tag"])
def test_capture_refuses_release_replacement_during_verification(
    release_capture, monkeypatch, tmp_path, change
):
    tag, blobs, metadata, commits, verifies, snapshots = release_capture

    def verify(payload, bundle):
        if change == "release":
            metadata["assets"][0]["id"] += 1
        else:
            commits.append("c" * 40)

    monkeypatch.setattr(capture, "verify_update", verify)
    with pytest.raises(ValueError, match="changed during capture"):
        capture.capture(tag, tmp_path / "capture")
    assert not json.loads((tmp_path / "capture/capture.json").read_text())["passed"]


def test_failed_signature_never_marks_capture_passed(release_capture, monkeypatch, tmp_path):
    from scripts.sidecar_pkg.signatures import UpdateVerificationError

    def reject(*args):
        raise UpdateVerificationError("refused")

    monkeypatch.setattr(capture, "verify_update", reject)
    with pytest.raises(UpdateVerificationError):
        capture.capture(release_capture[0], tmp_path / "capture")
    assert not json.loads((tmp_path / "capture/capture.json").read_text())["passed"]


def test_download_overflow_removes_partial_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "MAX_ARCHIVE_BYTES", 5)
    monkeypatch.setattr(capture.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"overflow"))
    with pytest.raises(ValueError, match="size limit"):
        capture.download("v3.0.0", "payload.zip", tmp_path / "payload.zip")
    assert not (tmp_path / "payload.zip").exists()


@pytest.mark.parametrize(
    "broken", ["valid", "tampered", "missing-bundle", "malformed-bundle", "oversized", None]
)
def test_frozen_smoke_requires_expected_results(monkeypatch, tmp_path, broken):
    import subprocess

    binary, payload, bundle = (tmp_path / name for name in ("binary", "payload", "bundle"))
    for path in (binary, payload, bundle):
        path.write_bytes(b"fixture")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        archive, verification_bundle = Path(command[2]), Path(command[4])
        name = (
            "tampered"
            if archive.name == "tampered.txt"
            else "oversized"
            if archive.name == "oversized.zip"
            else "missing-bundle"
            if verification_bundle.name == "missing.json"
            else "malformed-bundle"
            if verification_bundle.name == "malformed.json"
            else "valid"
        )
        code = 0 if name == "valid" else 1
        diagnostic = (
            "Update archive exceeds size limit"
            if name == "oversized"
            else "Update signature verification failed"
            if name != "valid"
            else ""
        )
        if broken == name:
            code = 1 if name == "valid" else 0
        assert kwargs["env"]["RUNWAY_CONFIG_DIR"] != str(tmp_path)
        return subprocess.CompletedProcess(command, code, "", diagnostic)

    monkeypatch.setattr(packaging.subprocess, "run", run)
    monkeypatch.setattr(packaging.subprocess, "check_output", lambda *a, **kw: "b" * 40)
    report = tmp_path / "evidence.json"
    if broken:
        with pytest.raises(RuntimeError, match=broken):
            packaging.smoke(binary, payload, bundle, report)
        assert not json.loads(report.read_text())["passed"]
    else:
        packaging.smoke(binary, payload, bundle, report)
        assert len(calls) == 5 and json.loads(report.read_text())["passed"]
