"""The exception document must describe the scan it was written against.

``test_image_policy.py`` checks the document's own shape: every entry has its
fields, the architectures are supported, the evidence is HTTPS, the review
window is positive and at most 30 days, and there are no duplicates. Shape
says nothing about whether the document still matches the image.

Two things go wrong that shape cannot see. An entry that matches no finding
waives nothing while reading exactly like a reviewed decision, and a finding
with no entry blocks the promotion. Both are invisible until an audit runs.

So the document is checked here against the scan it was written against,
replayed through the real policy engine rather than compared by hand. The scan
is stored in ``tests/fixtures/image-exceptions/``; regenerate it with
``python -m scripts.record_image_scan`` in the same pull request that
re-reviews the document.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.image_policy import BLOCKING_SEVERITIES, evaluate, exception_key, load_exceptions
from scripts.record_image_scan import record

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_EXCEPTIONS = REPO_ROOT / "docs" / "security" / "image-exceptions.json"
RECORDED_SCAN = REPO_ROOT / "tests" / "fixtures" / "image-exceptions" / "published-edge.json"
REMEDIATION = REPO_ROOT / "docs" / "security" / "image-remediation.md"
ARCHITECTURES = ("amd64", "arm64")


def _scan() -> dict:
    return json.loads(RECORDED_SCAN.read_text())


def _document() -> dict:
    return json.loads(SHIPPED_EXCEPTIONS.read_text())


def _published_image_section() -> str:
    """The remediation evidence's section for the published Debian 13 image."""
    remediation = REMEDIATION.read_text()
    heading = "### The published Debian 13 image"
    start = remediation.index(heading)
    end = remediation.index("\n### ", start + len(heading))
    return remediation[start:end]


class TestTheRecordedScan:
    def test_it_records_where_it_came_from(self):
        scan = _scan()

        assert scan["image"].startswith("ghcr.io/s3ntin3l8/runway@sha256:")
        assert scan["scanned_at"]
        assert scan["database"]["built"].endswith("Z")
        assert scan["database"]["checksum"].startswith("sha256:")

    def test_it_covers_both_architectures(self):
        assert set(_scan()["architectures"]) == set(ARCHITECTURES)

    def test_its_counts_match_the_findings_it_records(self):
        """The stored counts are the scan's; the stored findings are the blocking ones."""
        for architecture, recorded in _scan()["architectures"].items():
            severities = [match["vulnerability"]["severity"] for match in recorded["matches"]]

            assert set(severities) <= BLOCKING_SEVERITIES, architecture
            for severity, count in recorded["counts"].items():
                if severity in BLOCKING_SEVERITIES:
                    assert severities.count(severity) == count, f"{architecture} {severity}"

    def test_the_image_it_records_is_the_image_the_repository_records(self):
        """A fixture that has rotted away from its image cannot pass for ever.

        The remediation evidence records three images -- beta.8, the baseline
        it replaced, and the Debian 13 one -- so the check is scoped to the
        section this fixture claims to record. Otherwise a fixture of a
        superseded image would still pass.
        """
        scan = _scan()
        section = _published_image_section()

        digests = [scan["image"]] + [
            recorded["image"] for recorded in scan["architectures"].values()
        ]
        for reference in digests:
            digest = reference.rsplit("@", 1)[-1]
            assert digest in section, f"{digest} is not recorded in the published image section"

    def test_it_records_findings(self):
        """A scan that recorded nothing would pass every check below."""
        for architecture, recorded in _scan()["architectures"].items():
            assert recorded["matches"], architecture


class TestTheDocumentMatchesTheScan:
    @pytest.mark.parametrize("architecture", ARCHITECTURES)
    def test_the_policy_passes_over_the_recorded_scan(self, architecture):
        """The real engine, over the real findings, with the shipped document.

        A blocked finding is a finding with no entry, so this covers both
        halves of the gap at once: nothing is waived that the scan reported,
        and nothing the scan reported is left unwaived.
        """
        scan = _scan()
        loaded = load_exceptions(SHIPPED_EXCEPTIONS, datetime.now(UTC))

        verdict = evaluate(
            {"matches": scan["architectures"][architecture]["matches"]}, architecture, loaded
        )

        assert verdict["blocked"] == [], verdict["blocked"]
        assert verdict["passed"]
        assert len(verdict["exceptions"]) == len(scan["architectures"][architecture]["matches"])

    @pytest.mark.parametrize("architecture", ARCHITECTURES)
    def test_no_entry_waives_nothing(self, architecture):
        """An entry that matches no finding is a false record."""
        scan = _scan()
        document = _document()
        recorded = {
            (
                match["vulnerability"]["id"],
                match["vulnerability"]["namespace"],
                match["artifact"]["name"],
                match["artifact"]["version"],
                architecture,
            )
            for match in scan["architectures"][architecture]["matches"]
        }
        shipped = {
            exception_key(entry)
            for entry in document["exceptions"]
            if entry["architecture"] == architecture
        }

        assert shipped == recorded, (
            "the shipped exception document and the recorded scan disagree for "
            f"{architecture}: "
            f"{sorted(shipped - recorded)} waive nothing, "
            f"{sorted(recorded - shipped)} have no entry"
        )


def test_the_document_and_the_scan_cover_the_same_advisories():
    """Both records describe the same set of advisories."""
    scan = _scan()
    document = _document()

    recorded = {
        match["vulnerability"]["id"]
        for architecture in ARCHITECTURES
        for match in scan["architectures"][architecture]["matches"]
    }
    shipped = {entry["id"] for entry in document["exceptions"]}

    assert shipped == recorded


class TestRecordingAScan:
    """The recorder has to work on the layouts the workflows actually retain."""

    def _audit_dir(self, tmp_path, architectures=("amd64", "arm64")) -> Path:
        source = tmp_path / "image-audit"
        source.mkdir()
        (source / "evidence.json").write_text(
            json.dumps(
                {
                    "image": "ghcr.io/s3ntin3l8/runway@sha256:" + "a" * 64,
                    "scanned_at": "2026-10-23T17:42:48Z",
                    "database_listing": {
                        "built": "2026-10-23T06:30:15Z",
                        "checksum": "sha256:" + "b" * 64,
                    },
                    "platforms": {
                        architecture: {
                            "image": "ghcr.io/s3ntin3l8/runway@sha256:" + "c" * 64,
                            "counts": {"High": 1},
                        }
                        for architecture in architectures
                    },
                }
            )
        )
        for architecture in architectures:
            (source / f"{architecture}.vulnerabilities.json").write_text(
                json.dumps(
                    {
                        "matches": [
                            {
                                "vulnerability": {
                                    "id": "CVE-2026-0001",
                                    "namespace": "debian:distro:debian:13",
                                    "severity": "High",
                                    "fix": {"state": "wont-fix"},
                                },
                                "artifact": {
                                    "name": "example",
                                    "version": "1.0",
                                    "type": "deb",
                                },
                            }
                        ]
                    }
                )
            )
        return source

    def test_it_records_both_architectures(self, tmp_path):
        recorded = record(self._audit_dir(tmp_path))

        assert set(recorded["architectures"]) == set(ARCHITECTURES)
        assert recorded["image"].startswith("ghcr.io/s3ntin3l8/runway@sha256:")
        assert recorded["database"]["built"] == "2026-10-23T06:30:15Z"

    def test_it_records_the_findings_the_policy_reads(self, tmp_path):
        recorded = record(self._audit_dir(tmp_path))

        match = recorded["architectures"]["amd64"]["matches"][0]
        assert match["vulnerability"]["id"] == "CVE-2026-0001"
        assert match["artifact"]["name"] == "example"

    def test_it_leaves_out_findings_that_do_not_block(self, tmp_path):
        source = self._audit_dir(tmp_path)
        report = json.loads((source / "amd64.vulnerabilities.json").read_text())
        report["matches"].append(
            {
                "vulnerability": {
                    "id": "CVE-2026-0002",
                    "namespace": "debian:distro:debian:13",
                    "severity": "Medium",
                    "fix": {"state": "wont-fix"},
                },
                "artifact": {"name": "example", "version": "1.0", "type": "deb"},
            }
        )
        (source / "amd64.vulnerabilities.json").write_text(json.dumps(report))

        recorded = record(source)

        assert [
            m["vulnerability"]["id"] for m in recorded["architectures"]["amd64"]["matches"]
        ] == ["CVE-2026-0001"]

    def test_it_refuses_a_directory_without_an_audit(self, tmp_path):
        with pytest.raises(ValueError, match="No audit directory"):
            record(tmp_path / "missing")

    def test_it_refuses_a_directory_missing_an_architecture(self, tmp_path):
        source = self._audit_dir(tmp_path)
        (source / "arm64.vulnerabilities.json").unlink()

        with pytest.raises(ValueError, match="no arm64.vulnerabilities.json"):
            record(source)

    def test_it_refuses_evidence_without_a_database_listing(self, tmp_path):
        """A cache with no listing.json records it as null, not as an empty mapping."""
        source = self._audit_dir(tmp_path)
        evidence = json.loads((source / "evidence.json").read_text())
        evidence["database_listing"] = None
        (source / "evidence.json").write_text(json.dumps(evidence))

        with pytest.raises(ValueError, match="is not a mapping"):
            record(source)

    def test_it_refuses_evidence_without_the_keys_it_records(self, tmp_path):
        source = self._audit_dir(tmp_path)
        evidence = json.loads((source / "evidence.json").read_text())
        del evidence["image"]
        (source / "evidence.json").write_text(json.dumps(evidence))

        with pytest.raises(ValueError, match="has no 'image'"):
            record(source)
