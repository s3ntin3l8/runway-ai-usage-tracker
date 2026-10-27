"""Record an image scan as the fixture the exception document is checked against.

The exception document is only meaningful if it describes the findings the
scanner actually reported for the image: an entry that matches nothing is a
false record, and a finding with no entry blocks the promotion. Neither shows
up in the document's own shape, so the suite checks the document against a
stored scan instead -- see ``tests/unit/test_image_exceptions_scan.py``.

This script turns an ``image-audit`` directory into that stored scan. The
workflows retain one in three shapes, and the difference decides where the two
architectures are:

- ``immutable-image-audit`` and ``release-image-audit`` (since the move to the
  shared publish workflow, ``release-image-audit-<image-slug>``) audit an image index
  in one go, so their directory holds ``amd64.vulnerabilities.json``,
  ``arm64.vulnerabilities.json`` and one ``evidence.json`` side by side. This
  is the shape to record from: both architectures of one published image.
- ``published-image-security`` holds one directory per target (``edge``,
  ``release``), each with both architectures and its own ``evidence.json``.
  Pass the target with ``--target``.
- ``image-security-amd64`` and ``image-security-arm64`` are the candidate jobs,
  and each holds one architecture. Neither can produce this fixture, which
  records both architectures of one image, so the recorder does not take them.

Regenerate the fixture in the same pull request that re-reviews the document,
never on its own: a fixture that drifts from the scan it records would check
the document against a fiction.

Usage:
    python -m scripts.record_image_scan \\
        --audit-dir image-audit \\
        --output tests/fixtures/image-exceptions/published-release.json
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


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    """Read a key that has to be there, loudly.

    A missing key would otherwise become a null in the fixture, and a fixture
    with a null in it is a fixture that has quietly stopped recording
    anything.
    """
    if not isinstance(mapping, dict):
        raise ValueError(f"{where} is not a mapping")
    if key not in mapping:
        raise ValueError(f"{where} has no {key!r}")
    return mapping[key]


def record(audit_dir: Path, target: str | None = None) -> dict[str, Any]:
    """Build the fixture from an ``image-audit`` directory."""
    source = audit_dir / target if target else audit_dir
    if not source.is_dir():
        raise ValueError(f"No audit directory at {source}")

    evidence = json.loads((source / "evidence.json").read_text())
    listing = _require(evidence, "database_listing", f"{source}/evidence.json")
    fixture: dict[str, Any] = {
        "schema_version": 1,
        "image": _require(evidence, "image", f"{source}/evidence.json"),
        "scanned_at": _require(evidence, "scanned_at", f"{source}/evidence.json"),
        "database": {
            "built": _require(listing, "built", f"{source}/evidence.json"),
            "checksum": _require(listing, "checksum", f"{source}/evidence.json"),
        },
        "architectures": {},
    }
    platforms = _require(evidence, "platforms", f"{source}/evidence.json")
    for architecture in ARCHITECTURES:
        report_path = source / f"{architecture}.vulnerabilities.json"
        if not report_path.is_file():
            raise ValueError(
                f"{source} has no {architecture}.vulnerabilities.json; it holds "
                f"{sorted(path.name for path in source.glob('*.vulnerabilities.json'))}"
            )
        report = json.loads(report_path.read_text())
        blocking = [
            _reduced(match)
            for match in report["matches"]
            if match["vulnerability"]["severity"] in BLOCKING_SEVERITIES
        ]
        platform = _require(platforms, architecture, f"{source}/evidence.json")
        fixture["architectures"][architecture] = {
            "image": _require(platform, "image", f"{source}/evidence.json platforms"),
            "counts": _require(platform, "counts", f"{source}/evidence.json platforms"),
            "matches": blocking,
        }
    return fixture


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    audit_dir, output = Path("image-audit"), Path("tests/fixtures/image-exceptions/scan.json")
    target = None
    index = 0
    while index < len(argv):
        option = argv[index]
        if option == "--audit-dir" and index + 1 < len(argv):
            audit_dir = Path(argv[index + 1])
            index += 2
        elif option == "--output" and index + 1 < len(argv):
            output = Path(argv[index + 1])
            index += 2
        elif option == "--target" and index + 1 < len(argv):
            target = argv[index + 1]
            index += 2
        else:
            print(__doc__)
            return 2
    try:
        fixture = record(audit_dir, target)
    except (OSError, TypeError, ValueError) as exc:
        print(f"Cannot record the scan: {exc}")
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
    for architecture, recorded in fixture["architectures"].items():
        print(f"{architecture}: {len(recorded['matches'])} blocking findings -> {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
