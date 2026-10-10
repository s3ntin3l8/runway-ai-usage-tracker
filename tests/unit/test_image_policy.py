"""Release gates fail closed without hiding scanner evidence."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts import audit_release_image as audit_module
from scripts.image_policy import check_database, evaluate, load_exceptions

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def database(**overrides):
    return {
        "built": NOW.isoformat(),
        "valid": True,
        "from": "https://grype.anchore.io/db?checksum=sha256:" + "a" * 64,
        **overrides,
    }


def finding(severity="High", state="not-fixed", kind="deb"):
    return {
        "vulnerability": {
            "id": "CVE-2026-TEST",
            "namespace": "debian:12",
            "severity": severity,
            "fix": {"state": state},
        },
        "artifact": {"name": "test-package", "version": "1.0", "type": kind},
    }


def exception():
    return {
        "id": "CVE-2026-TEST",
        "namespace": "debian:12",
        "package": "test-package",
        "version": "1.0",
        "architecture": "amd64",
        "reason": "Test applicability evidence",
        "evidence": "https://security-tracker.debian.org/tracker/CVE-2026-TEST",
        "owner": "reviewer",
        "issue": "https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/602",
        "reviewed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
    }


def write_exceptions(tmp_path, entries):
    path = tmp_path / "exceptions.json"
    path.write_text(json.dumps({"schema_version": 1, "exceptions": entries}))
    return path


@pytest.mark.parametrize("severity", ["Critical", "High"])
@pytest.mark.parametrize("state", ["fixed", "not-fixed", "wont-fix", "unknown"])
def test_blocks_without_applicability_review(severity, state):
    report = {"matches": [finding(severity, state)]}
    result = evaluate(report, "amd64", {})
    assert not result["passed"]
    assert result["blocked"][0]["fix_state"] == state
    assert report["matches"]  # raw evidence remains untouched


def test_lower_severities_and_python_are_reported():
    result = evaluate({"matches": [finding("Medium", kind="python")]}, "arm64", {})
    assert result["passed"]
    assert result["counts"] == {"Medium": 1}
    assert len(result["python_findings"]) == 1


def test_exception_only_accepts_exact_package_version_arch_and_advisory(tmp_path):
    exceptions = load_exceptions(write_exceptions(tmp_path, [exception()]), NOW)
    report = {"matches": [finding()]}
    assert evaluate(report, "amd64", exceptions)["passed"]
    assert not evaluate(report, "arm64", exceptions)["passed"]
    for obj, field in (
        (report["matches"][0]["artifact"], "version"),
        (report["matches"][0]["artifact"], "name"),
        (report["matches"][0]["vulnerability"], "id"),
        (report["matches"][0]["vulnerability"], "namespace"),
    ):
        original = obj[field]
        obj[field] = "different"
        assert not evaluate(report, "amd64", exceptions)["passed"]
        obj[field] = original


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": NOW.isoformat()},
        {"expires_at": (NOW + timedelta(days=31)).isoformat()},
        {"reviewed_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"architecture": "*"},
        {"owner": ""},
        {"reason": ""},
        {"evidence": "http://example.com"},
        {"issue": "https://example.com"},
        {"reviewed_at": "2026-10-10"},
    ],
)
def test_rejects_unreviewed_or_expired_exceptions(tmp_path, change):
    with pytest.raises(ValueError):
        load_exceptions(write_exceptions(tmp_path, [{**exception(), **change}]), NOW)


def test_duplicate_exception_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_exceptions(write_exceptions(tmp_path, [exception(), exception()]), NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {"built": (NOW - timedelta(hours=24, seconds=1)).isoformat()},
        {"built": (NOW + timedelta(minutes=6)).isoformat()},
        {"built": "2026-10-10"},
        {"valid": False},
        {"error": "failure"},
        {"from": ""},
    ],
)
def test_database_fails_closed(overrides):
    with pytest.raises(ValueError):
        check_database(database(**overrides), NOW)


def test_fresh_database_accepted():
    check_database(database(built=(NOW - timedelta(hours=24)).isoformat()), NOW)


@pytest.mark.parametrize("report", [{}, {"matches": None}, {"matches": [{}]}])
def test_incomplete_report_rejected(report):
    with pytest.raises((ValueError, KeyError)):
        evaluate(report, "amd64", {})


def test_scan_failure_retains_failed_evidence(tmp_path, monkeypatch):
    exceptions = write_exceptions(tmp_path, [])
    monkeypatch.setattr(audit_module, "prepare_database", lambda _: database())
    monkeypatch.setattr(audit_module, "check_database", lambda *args: None)
    monkeypatch.setattr(
        audit_module,
        "scan_archive",
        lambda *args: (_ for _ in ()).throw(RuntimeError("scanner failed")),
    )
    output = tmp_path / "audit"
    with pytest.raises(RuntimeError, match="scanner failed"):
        audit_module.audit(
            None,
            output,
            archive=tmp_path / "image.tar",
            architecture="amd64",
            exceptions_path=exceptions,
        )
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["passed"] is False
    assert evidence["error"] == "scanner failed"


def test_missing_architecture_never_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_module, "prepare_database", lambda _: database())
    monkeypatch.setattr(audit_module, "check_database", lambda *args: None)
    monkeypatch.setattr(
        audit_module.subprocess,
        "check_output",
        lambda *args, **kwargs: json.dumps(
            {
                "manifests": [
                    {
                        "platform": {"os": "linux", "architecture": "amd64"},
                        "digest": "sha256:" + "a" * 64,
                    }
                ]
            }
        ),
    )
    output = tmp_path / "audit"
    with pytest.raises(ValueError, match="both"):
        audit_module.audit(
            "ghcr.io/s3ntin3l8/runway@sha256:" + "a" * 64,
            output,
            exceptions_path=write_exceptions(tmp_path, []),
        )
    assert json.loads((output / "evidence.json").read_text())["passed"] is False


@pytest.mark.parametrize("severity", ["HIGH", "", None, "unrecognized"])
def test_unknown_severity_cannot_bypass_policy(severity):
    with pytest.raises(ValueError, match="severity"):
        evaluate({"matches": [finding(severity)]}, "amd64", {})


@pytest.mark.parametrize(
    "extra",
    [
        {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:" + "b" * 64},
        {"platform": {"os": "linux", "architecture": "ppc64le"}, "digest": "sha256:" + "b" * 64},
        {"platform": {"os": "windows", "architecture": "amd64"}, "digest": "sha256:" + "b" * 64},
        {"platform": {"os": "unknown", "architecture": "unknown"}, "digest": "sha256:" + "b" * 64},
    ],
)
def test_index_cannot_promote_unscanned_platforms(extra):
    base = [
        {"platform": {"os": "linux", "architecture": arch}, "digest": "sha256:" + "a" * 64}
        for arch in ("amd64", "arm64")
    ]
    with pytest.raises(ValueError, match="platform"):
        audit_module.executable_manifests({"manifests": base + [extra]})


def test_exact_index_allows_attestations():
    manifests = [
        {"platform": {"os": "linux", "architecture": arch}, "digest": "sha256:" + "a" * 64}
        for arch in ("amd64", "arm64")
    ]
    manifests.append(
        {
            "platform": {"os": "unknown", "architecture": "unknown"},
            "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
        }
    )
    assert set(audit_module.executable_manifests({"manifests": manifests})) == {"amd64", "arm64"}


def test_empty_sbom_cannot_pass(tmp_path, monkeypatch):
    def fake_run(*args, stdout=None):
        stdout.write(json.dumps({"components": []}))

    monkeypatch.setattr(audit_module, "run", fake_run)
    with pytest.raises(ValueError, match="empty package"):
        audit_module.scan_archive(tmp_path / "image.tar", "amd64", tmp_path, tmp_path)
