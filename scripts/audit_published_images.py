"""Resolve moving tags once, then audit the resulting immutable bytes."""

import argparse
import json
import re
import subprocess
from pathlib import Path

from scripts.audit_release_image import audit


def audit_published(release: str, output: Path) -> bool:
    if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", release):
        raise ValueError("Invalid release version")
    failed = False
    for label, tag in (("edge", "edge"), ("release", release.removeprefix("v"))):
        try:
            raw = subprocess.check_output(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    f"ghcr.io/s3ntin3l8/runway:{tag}",
                    "--format",
                    "{{json .Manifest.Digest}}",
                ],
                text=True,
                timeout=60,
            )
            digest = json.loads(raw)
            failed = audit(f"ghcr.io/s3ntin3l8/runway@{digest}", output / label) or failed
        except Exception as exc:
            directory = output / label
            directory.mkdir(parents=True, exist_ok=True)
            evidence = directory / "evidence.json"
            if not evidence.exists():
                evidence.write_text(
                    json.dumps({"passed": False, "tag": tag, "error": str(exc)}, indent=2)
                )
            print(f"Published image audit failed for {label}")
            # Still collect the other image; an audit failure never becomes success.
            failed = True
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(int(audit_published(args.release, args.output)))
