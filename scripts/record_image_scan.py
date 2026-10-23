"""Record an image scan as the fixture the exception document is checked against.

The exception document is only meaningful if it describes the findings the
scanner actually reported for the image: an entry that matches nothing is a
false record, and a finding with no entry blocks the promotion. Neither shows
up in the document's own shape, so the suite checks the document against a
stored scan instead -- see ``tests/unit/test_image_exceptions_scan.py``.

This script turns an ``image-audit`` directory into that stored scan. The
candidate and published audit jobs retain one, and it holds the unfiltered
Grype reports and the evidence for both architectures.

Regenerate the fixture in the same pull request that re-reviews the document,
never on its own: a fixture that drifts from the scan it records would check
the document against a fiction.

Usage:
    python -m scripts.record_image_scan \\
        --audit-dir image-audit \\
        --output tests/fixtures/image-exceptions/published-edge.json
"""

import json
import sys
from pathlib import Path
from typing import Any

from scripts.image_policy import BLOCKING_SEVERITIES

ARCHITECTURES = ("amd64", "arm64")


def _reduced(match: dict[str, Any]) -> dict[str, Any]:
    """The part of a Grype match the policy engine actually reads."""
    vulnerability, artifact = match["vulnerability"], match["artifact"]
    return {
        "vulnerability": {
            "id": vulnerability["id"],
            "namespace": vulnerability["namespace"],
            "severity": vulnerability["severity"],
            "fix": {"state": vulnerability.get("fix", {}).get("state", "unknown")},
        },
        "artifact": {
            "name": artifact["name"],
            "version": artifact["version"],
            "type": artifact.get("type", ""),
        },
    }


def record(audit_dir: Path) -> dict[str, Any]:
    """Build the fixture from an ``image-audit`` directory."""
    evidence = json.loads((audit_dir / "evidence.json").read_text())
    listing = evidence.get("database_listing", {})
    fixture: dict[str, Any] = {
        "schema_version": 1,
        "image": evidence.get("image"),
        "scanned_at": evidence.get("scanned_at"),
        "database": {
            "built": listing.get("built"),
            "checksum": listing.get("checksum"),
        },
        "architectures": {},
    }
    for architecture in ARCHITECTURES:
        report = json.loads((audit_dir / f"{architecture}.vulnerabilities.json").read_text())
        blocking = [
            _reduced(match)
            for match in report["matches"]
            if match["vulnerability"]["severity"] in BLOCKING_SEVERITIES
        ]
        platform = evidence.get("platforms", {}).get(architecture, {})
        fixture["architectures"][architecture] = {
            "image": platform.get("image"),
            "counts": platform.get("counts", {}),
            "matches": blocking,
        }
    return fixture


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    audit_dir, output = Path("image-audit"), Path("tests/fixtures/image-exceptions/scan.json")
    index = 0
    while index < len(argv):
        option = argv[index]
        if option == "--audit-dir" and index + 1 < len(argv):
            audit_dir = Path(argv[index + 1])
            index += 2
        elif option == "--output" and index + 1 < len(argv):
            output = Path(argv[index + 1])
            index += 2
        else:
            print(__doc__)
            return 2
    if not audit_dir.is_dir():
        print(f"No audit directory at {audit_dir}")
        return 1
    fixture = record(audit_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
    for architecture, recorded in fixture["architectures"].items():
        print(f"{architecture}: {len(recorded['matches'])} blocking findings -> {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
