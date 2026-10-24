"""Release gates fail closed without hiding scanner evidence."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import audit_release_image as audit_module
from scripts.image_policy import (
    ExceptionSet,
    check_database,
    evaluate,
    load_exceptions,
    renewal_due,
)

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_EXCEPTIONS = REPO_ROOT / "docs" / "security" / "image-exceptions.json"


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


def exception(**overrides):
    entry = {
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
    entry.update(overrides)
    return entry


def expired_exception(**overrides):
    """An entry whose review window closed before NOW."""
    return exception(
        reviewed_at=(NOW - timedelta(days=30)).isoformat(),
        expires_at=(NOW - timedelta(days=1)).isoformat(),
        **overrides,
    )


def write_exceptions(tmp_path, entries):
    path = tmp_path / "exceptions.json"
    path.write_text(json.dumps({"schema_version": 1, "exceptions": entries}))
    return path


@pytest.mark.parametrize("severity", ["Critical", "High"])
@pytest.mark.parametrize("state", ["fixed", "not-fixed", "wont-fix", "unknown"])
def test_blocks_without_applicability_review(severity, state):
    report = {"matches": [finding(severity, state)]}
    result = evaluate(report, "amd64", ExceptionSet())
    assert not result["passed"]
    assert result["blocked"][0]["fix_state"] == state
    assert report["matches"]  # raw evidence remains untouched


def test_lower_severities_and_python_are_reported():
    result = evaluate({"matches": [finding("Medium", kind="python")]}, "arm64", ExceptionSet())
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
def test_rejects_malformed_exceptions(tmp_path, change):
    with pytest.raises(ValueError):
        load_exceptions(write_exceptions(tmp_path, [{**exception(), **change}]), NOW)


def test_malformed_exception_names_the_offending_entry(tmp_path):
    with pytest.raises(ValueError, match="CVE-2026-TEST test-package 1.0 amd64"):
        load_exceptions(write_exceptions(tmp_path, [exception(evidence="http://example.com")]), NOW)


def test_duplicate_exception_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_exceptions(write_exceptions(tmp_path, [exception(), exception()]), NOW)


def test_duplicate_of_an_expired_entry_is_still_rejected(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        load_exceptions(write_exceptions(tmp_path, [expired_exception(), exception()]), NOW)


def test_expired_entry_is_reported_instead_of_invalidating_the_document(tmp_path):
    loaded = load_exceptions(write_exceptions(tmp_path, [expired_exception()]), NOW)
    assert loaded.active == {}
    assert len(loaded.expired) == 1
    entry = next(iter(loaded.expired.values()))
    assert entry["id"] == "CVE-2026-TEST"


def test_expired_entry_blocks_its_finding_and_names_the_expiry(tmp_path):
    loaded = load_exceptions(write_exceptions(tmp_path, [expired_exception()]), NOW)
    result = evaluate({"matches": [finding()]}, "amd64", loaded)
    assert not result["passed"]
    blocked = result["blocked"][0]
    assert blocked["id"] == "CVE-2026-TEST"
    assert blocked["expired_exception"]["expires_at"] == (NOW - timedelta(days=1)).isoformat()
    assert blocked["expired_exception"]["package"] == "test-package"
    assert blocked["expired_exception"]["architecture"] == "amd64"


def test_expired_entry_does_not_invalidate_the_other_entries(tmp_path):
    loaded = load_exceptions(
        write_exceptions(tmp_path, [expired_exception(package="gone-package"), exception()]), NOW
    )
    report = {"matches": [finding()]}
    # The live entry still waives the finding, even though a sibling lapsed.
    assert evaluate(report, "amd64", loaded)["passed"]
    assert len(loaded.active) == 1
    assert len(loaded.expired) == 1


def test_expired_entry_for_an_absent_package_does_not_apply_to_other_findings(tmp_path):
    loaded = load_exceptions(
        write_exceptions(tmp_path, [expired_exception(package="gone-package")]), NOW
    )
    result = evaluate({"matches": [finding()]}, "amd64", loaded)
    # Nothing excepts this finding, so it blocks -- but the lapsed entry for a
    # different package must not be credited to it.
    assert not result["passed"]
    assert "expired_exception" not in result["blocked"][0]


def test_expired_entry_cannot_waive_a_finding_on_another_architecture(tmp_path):
    loaded = load_exceptions(write_exceptions(tmp_path, [expired_exception()]), NOW)
    result = evaluate({"matches": [finding()]}, "arm64", loaded)
    assert not result["passed"]
    assert "expired_exception" not in result["blocked"][0]


def test_shipped_exception_document_needs_no_renewal():
    now = datetime.now(UTC)
    loaded = load_exceptions(SHIPPED_EXCEPTIONS, now)
    due = renewal_due(loaded, now)
    assert due == [], (
        "these shipped exceptions are lapsed or expire within the warning window and "
        "must be renewed (see docs/security/image-policy.md): "
        + ", ".join(
            f"{entry['id']} {entry['package']} {entry['version']} {entry['architecture']}"
            for entry in due
        )
    )


def test_shipped_exception_document_has_a_single_expiry_horizon():
    horizons = {
        entry["expires_at"]
        for entry in load_exceptions(SHIPPED_EXCEPTIONS, datetime.now(UTC)).active.values()
    }
    assert len(horizons) == 1, (
        "the shipped exception document must have a single expiry horizon, so the whole "
        "document is re-reviewed on one cycle; give a new entry the same expires_at as the "
        f"rest of the document. Found several: {sorted(horizons)}"
    )


def test_scheduled_exception_check_passes_on_the_shipped_document():
    from scripts import check_image_exceptions

    assert check_image_exceptions.main() == 0


def test_scheduled_exception_check_reports_a_lapse(tmp_path, monkeypatch):
    from scripts import check_image_exceptions

    lapsed = write_exceptions(tmp_path, [expired_exception()])
    monkeypatch.setattr(check_image_exceptions, "DEFAULT_EXCEPTIONS", lapsed)
    assert check_image_exceptions.main() == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"built": (NOW - timedelta(hours=48, seconds=1)).isoformat()},
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
    check_database(database(built=(NOW - timedelta(hours=48)).isoformat()), NOW)


def test_publication_lag_database_accepted():
    # The newest release upstream offers is up to ~25h old by its build stamp.
    check_database(database(built=(NOW - timedelta(hours=25, minutes=7)).isoformat()), NOW)


def test_a_two_day_old_database_is_accepted():
    """Two days of upstream silence is where the tripwire fires, not one."""
    check_database(database(built=(NOW - timedelta(hours=47)).isoformat()), NOW)
    with pytest.raises(ValueError):
        check_database(database(built=(NOW - timedelta(hours=48, seconds=1)).isoformat()), NOW)


def test_the_upstream_stall_of_2026_10_24_is_tolerated():
    """The release upstream's 2026-10-24 stall left behind is 35h old."""
    check_database(database(built=(NOW - timedelta(hours=35, minutes=12)).isoformat()), NOW)


def test_database_must_be_the_advertised_release():
    with pytest.raises(ValueError, match="currently advertises"):
        check_database(database(), NOW, "sha256:" + "b" * 64)
    check_database(database(), NOW, "sha256:" + "a" * 64)


def test_identity_check_is_optional_but_age_is_not():
    check_database(database(built=(NOW - timedelta(hours=25)).isoformat()), NOW, None)
    with pytest.raises(ValueError):
        check_database(database(built=(NOW - timedelta(hours=49)).isoformat()), NOW, None)


def scanner_stub(state):
    """Serve canned status and listing payloads, one listing per update attempt."""

    def scanner(cache, *args, stdout=None):
        command = args[:2]
        if command == ("db", "update"):
            state["updates"] += 1
            return
        index = min(state["updates"] - 1, len(state["listings"]) - 1)
        payload = state["status"] if command == ("db", "status") else state["listings"][index]
        stdout.write(json.dumps(payload))

    return scanner


def advertised(checksum):
    return [{"status": "active", "checksum": checksum, "built": NOW.isoformat()}]


def test_read_listing_without_a_record_returns_none(tmp_path):
    assert audit_module.read_listing(tmp_path / "listing.json") is None


def test_prepare_database_keeps_the_advertised_release(tmp_path, monkeypatch):
    state = {"updates": 0, "status": database(), "listings": [advertised("sha256:" + "a" * 64)]}
    monkeypatch.setattr(audit_module, "scanner", scanner_stub(state))
    monkeypatch.setattr(audit_module, "sleep", lambda _: None)
    cache = tmp_path / "cache"
    status, listing = audit_module.prepare_database(cache)
    assert status == state["status"]
    assert listing == state["listings"][0][0]
    assert json.loads((cache / "listing.json").read_text()) == state["listings"][0]


def test_prepare_database_rejects_a_superseded_release(tmp_path, monkeypatch):
    state = {"updates": 0, "status": database(), "listings": [advertised("sha256:" + "b" * 64)]}
    monkeypatch.setattr(audit_module, "scanner", scanner_stub(state))
    monkeypatch.setattr(audit_module, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="currently advertises"):
        audit_module.prepare_database(tmp_path / "cache")
    assert state["updates"] == audit_module.DB_UPDATE_ATTEMPTS


def test_prepare_database_retries_after_upstream_publishes(tmp_path, monkeypatch):
    state = {
        "updates": 0,
        "status": database(),
        "listings": [advertised("sha256:" + "b" * 64), advertised("sha256:" + "a" * 64)],
    }
    monkeypatch.setattr(audit_module, "scanner", scanner_stub(state))
    monkeypatch.setattr(audit_module, "sleep", lambda _: None)
    status, listing = audit_module.prepare_database(tmp_path / "cache")
    assert status == state["status"]
    assert listing == state["listings"][1][0]
    assert state["updates"] == 2


@pytest.mark.parametrize(
    "listing",
    [
        [{"status": "deprecated", "checksum": "sha256:" + "a" * 64}],
        [{"status": "invalid", "checksum": "sha256:" + "a" * 64}],
        {"status": "active", "checksum": "sha256:" + "a" * 64},
        [],
    ],
)
def test_prepare_database_rejects_an_unusable_listing(tmp_path, monkeypatch, listing):
    state = {"updates": 0, "status": database(), "listings": [listing]}
    monkeypatch.setattr(audit_module, "scanner", scanner_stub(state))
    monkeypatch.setattr(audit_module, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="listing"):
        audit_module.prepare_database(tmp_path / "cache")
    assert state["updates"] == 1


@pytest.mark.parametrize("report", [{}, {"matches": None}, {"matches": [{}]}])
def test_incomplete_report_rejected(report):
    with pytest.raises((ValueError, KeyError)):
        evaluate(report, "amd64", ExceptionSet())


def test_scan_failure_retains_failed_evidence(tmp_path, monkeypatch):
    exceptions = write_exceptions(tmp_path, [])
    monkeypatch.setattr(
        audit_module,
        "prepare_database",
        lambda _: (database(), {"status": "active", "checksum": "sha256:" + "a" * 64}),
    )
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


def test_audit_records_the_expired_exceptions(tmp_path, monkeypatch):
    exceptions = write_exceptions(
        tmp_path, [expired_exception(package="gone-package"), exception()]
    )
    monkeypatch.setattr(
        audit_module,
        "prepare_database",
        lambda _: (database(), {"status": "active", "checksum": "sha256:" + "a" * 64}),
    )
    monkeypatch.setattr(audit_module, "check_database", lambda *args: None)
    monkeypatch.setattr(
        audit_module,
        "scan_archive",
        lambda *args, **kwargs: {"matches": [finding()]},
    )
    output = tmp_path / "audit"
    audit_module.audit(
        None,
        output,
        archive=tmp_path / "image.tar",
        architecture="amd64",
        exceptions_path=exceptions,
    )
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["expired_exceptions"] == [
        {
            "id": "CVE-2026-TEST",
            "package": "gone-package",
            "version": "1.0",
            "architecture": "amd64",
            "expires_at": (NOW - timedelta(days=1)).isoformat(),
        }
    ]
    # The live entry still waives the finding, so the audit passes.
    assert evidence["passed"] is True


def test_missing_architecture_never_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        audit_module,
        "prepare_database",
        lambda _: (database(), {"status": "active", "checksum": "sha256:" + "a" * 64}),
    )
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
        evaluate({"matches": [finding(severity)]}, "amd64", ExceptionSet())


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
