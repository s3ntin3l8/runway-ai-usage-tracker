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

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_EXCEPTIONS = REPO_ROOT / "docs" / "security" / "image-exceptions.json"
RECORDED_SCAN = REPO_ROOT / "tests" / "fixtures" / "image-exceptions" / "published-edge.json"
ARCHITECTURES = ("amd64", "arm64")


def _scan() -> dict:
    return json.loads(RECORDED_SCAN.read_text())


def _document() -> dict:
    return json.loads(SHIPPED_EXCEPTIONS.read_text())


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


class TestTheDocumentMatchesTheScan:
    @pytest.mark.parametrize("architecture", ARCHITECTURES)
    def test_the_policy_passes_over_the_recorded_scan(self, architecture):
        """The real engine, over the real findings, with the shipped document."""
        scan = _scan()
        loaded = load_exceptions(SHIPPED_EXCEPTIONS, datetime.now(UTC))

        verdict = evaluate(
            {"matches": scan["architectures"][architecture]["matches"]}, architecture, loaded
        )

        assert verdict["blocked"] == [], verdict["blocked"]
        assert verdict["passed"]

    @pytest.mark.parametrize("architecture", ARCHITECTURES)
    def test_every_blocking_finding_has_an_entry(self, architecture):
        """A finding with no entry blocks the promotion."""
        scan = _scan()
        loaded = load_exceptions(SHIPPED_EXCEPTIONS, datetime.now(UTC))

        verdict = evaluate(
            {"matches": scan["architectures"][architecture]["matches"]}, architecture, loaded
        )

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
