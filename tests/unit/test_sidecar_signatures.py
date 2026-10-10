from pathlib import Path

import pytest
from sigstore.models import TrustedRoot
from sigstore.verify import Verifier

from scripts.sidecar_pkg import self_update, signatures

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sigstore"


@pytest.fixture
def offline_verifier(monkeypatch):
    """Real production signatures and trust anchors; no live network in tests."""
    verifier = Verifier(trusted_root=TrustedRoot.from_file(FIXTURES / "trusted_root.json"))
    monkeypatch.setattr(Verifier, "production", lambda: verifier)


def test_real_repository_signature_with_expired_leaf(offline_verifier):
    # Certificate validity ended 2026-10-09; the verified transparency entry
    # establishes a valid signing time, so later verification still succeeds.
    signatures.verify_update(FIXTURES / "SHA256SUMS.txt", FIXTURES / "SHA256SUMS.txt.sigstore.json")


def test_release_gate_verifies_every_payload_with_production_policy(offline_verifier, tmp_path):
    from scripts.verify_release_updates import verify_release

    assert verify_release(tmp_path) == ["No portable update payloads found"]
    for name in ["Runway-Sidecar-Windows-edge.zip", "Runway-Sidecar-Linux-edge.tar.gz"]:
        payload = tmp_path / name
        payload.write_bytes((FIXTURES / "SHA256SUMS.txt").read_bytes())
        payload.with_name(name + ".sigstore.json").write_bytes(
            (FIXTURES / "SHA256SUMS.txt.sigstore.json").read_bytes()
        )
    assert verify_release(tmp_path) == []
    (tmp_path / "Runway-Sidecar-Windows-edge.zip").write_bytes(b"tampered")
    (tmp_path / "Runway-Sidecar-Linux-edge.tar.gz.sigstore.json").unlink()
    assert len(verify_release(tmp_path)) == 2


def test_release_gate_rejects_legacy_cosign_bundle(offline_verifier, tmp_path):
    from scripts.verify_release_updates import verify_release

    payload = tmp_path / "Runway-Sidecar-Windows-edge.zip"
    payload.write_bytes((FIXTURES / "SHA256SUMS.txt").read_bytes())
    payload.with_name(payload.name + ".sigstore.json").write_text(
        '{"base64Signature":"","cert":"","rekorBundle":{}}'
    )
    assert len(verify_release(tmp_path)) == 1


@pytest.mark.parametrize("failure", ["tamper", "identity", "issuer", "bundle", "oversize"])
def test_invalid_signature_fails_closed(offline_verifier, monkeypatch, tmp_path, failure):
    artifact = tmp_path / "artifact"
    artifact.write_bytes((FIXTURES / "SHA256SUMS.txt").read_bytes())
    bundle = tmp_path / "bundle.json"
    bundle.write_bytes((FIXTURES / "SHA256SUMS.txt.sigstore.json").read_bytes())
    if failure == "tamper":
        artifact.write_bytes(b"tampered payload")
    elif failure == "identity":
        monkeypatch.setattr(
            signatures,
            "SIGNING_IDENTITY",
            "https://github.com/other/repo/.github/workflows/sidecar-build.yml@refs/heads/main",
        )
    elif failure == "issuer":
        monkeypatch.setattr(signatures, "SIGNING_ISSUER", "https://attacker.test")
    elif failure == "bundle":
        bundle.write_bytes(b"{}")
    else:
        bundle.write_bytes(b"x" * (signatures.MAX_BUNDLE_BYTES + 1))
    with pytest.raises(signatures.UpdateVerificationError):
        signatures.verify_update(artifact, bundle)


def test_trust_refresh_failure_fails_closed(monkeypatch):
    def unavailable():
        raise OSError("Network unavailable")

    monkeypatch.setattr(Verifier, "production", unavailable)
    with pytest.raises(signatures.UpdateVerificationError):
        signatures.verify_update(
            FIXTURES / "SHA256SUMS.txt", FIXTURES / "SHA256SUMS.txt.sigstore.json"
        )


@pytest.mark.parametrize("valid", [True, False])
def test_verification_before_extraction_and_installation(monkeypatch, tmp_path, valid):
    calls = []
    monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
    monkeypatch.setattr(self_update, "_is_docker", lambda: False)
    monkeypatch.setattr(self_update, "running_from_disk_image", lambda: False)
    monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path)
    monkeypatch.setattr(self_update, "check_once", lambda *a: "9.0.0")
    monkeypatch.setattr(self_update, "_asset_name_candidates", lambda *a: ["payload.zip"])
    monkeypatch.setattr(
        self_update,
        "_get_release_json",
        lambda *a: {
            "tag_name": "v9.0.0",
            "assets": [
                {"name": n, "browser_download_url": "https://example.test/" + n}
                for n in ["payload.zip", "payload.zip.sha256", "payload.zip.sigstore.json"]
            ],
        },
    )
    monkeypatch.setattr(
        self_update, "_download", lambda url, dest, **kwargs: dest.write_bytes(b"payload")
    )
    monkeypatch.setattr(self_update, "_fetch_expected_sha", lambda *a: "expected")
    monkeypatch.setattr(self_update, "verify_sha256", lambda *a: True)

    def verify(*args):
        calls.append("verify")
        if not valid:
            raise signatures.UpdateVerificationError("invalid")

    monkeypatch.setattr(self_update, "verify_update", verify)
    monkeypatch.setattr(self_update, "_extract", lambda *a: calls.append("extract"))
    monkeypatch.setattr(
        self_update, "apply_update", lambda *a, **k: calls.append("install") or True
    )
    assert self_update.self_update("1.0.0", "stable", restart=False) is valid
    assert calls == (["verify", "extract", "install"] if valid else ["verify"])
