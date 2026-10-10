"""Catalog and scan immutable Runway image bytes on both supported architectures."""

import argparse
import json
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SYFT = "anchore/syft@sha256:3eb5379ba7b409c3f4069b686110527af0c47df993fa5c10d13e7cf34f49b1aa"
GRYPE = "anchore/grype@sha256:e4a44ef45d285b829ce6efe2642980329661bd2d18eab5fc539138d4adaebbbe"
IMAGE_PATTERN = r"ghcr\.io/s3ntin3l8/runway@sha256:[0-9a-f]{64}"


def audit(image: str, output: Path) -> bool:
    if not re.fullmatch(IMAGE_PATTERN, image):
        raise ValueError("Use ghcr.io/s3ntin3l8/runway@sha256:<64 lowercase hex characters>")
    output.mkdir(parents=True, exist_ok=True)
    raw = json.loads(
        subprocess.check_output(
            ["docker", "buildx", "imagetools", "inspect", "--raw", image], text=True
        )
    )
    manifests = {
        m.get("platform", {}).get("architecture"): m
        for m in raw.get("manifests", [])
        if m.get("platform", {}).get("os") == "linux"
    }
    if not {"amd64", "arm64"}.issubset(manifests):
        raise ValueError("Image must include both linux/amd64 and linux/arm64 manifests")
    evidence = {
        "image": image,
        "scanned_at": datetime.now(UTC).isoformat(),
        "syft": SYFT,
        "grype": GRYPE,
        "platforms": {},
    }
    vulnerable = False
    for arch in ("amd64", "arm64"):
        target = image.split("@", maxsplit=1)[0] + "@" + manifests[arch]["digest"]
        sbom_path = output / f"{arch}.cyclonedx.json"
        # Docker performs registry authentication; scanners receive only image bytes.
        subprocess.run(
            ["docker", "pull", "--platform", "linux/" + arch, target], check=True, timeout=600
        )
        with tempfile.TemporaryDirectory(prefix="runway-image-") as temp:
            archive = Path(temp) / "image.tar"
            subprocess.run(
                ["docker", "image", "save", "--output", str(archive), target],
                check=True,
                timeout=600,
            )
            with sbom_path.open("w") as fh:
                subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "-v",
                        f"{archive.resolve()}:/image.tar:ro",
                        SYFT,
                        "docker-archive:/image.tar",
                        "-o",
                        "cyclonedx-json",
                    ],
                    stdout=fh,
                    check=True,
                    timeout=600,
                )
        sbom = json.loads(sbom_path.read_text())
        inventory = [
            {"name": c["name"], "version": c.get("version"), "purl": c.get("purl")}
            for c in sbom.get("components", [])
        ]
        (output / f"{arch}.packages.json").write_text(json.dumps(inventory, indent=2))
        scan_path = output / f"{arch}.vulnerabilities.json"
        with scan_path.open("w") as fh:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{sbom_path.resolve()}:/input.json:ro",
                    GRYPE,
                    "sbom:/input.json",
                    "-o",
                    "json",
                ],
                stdout=fh,
                check=True,
                timeout=600,
            )
        matches = json.loads(scan_path.read_text())["matches"]
        vulnerable = vulnerable or bool(matches)
        evidence["platforms"][arch] = {
            "image": target,
            "packages": len(inventory),
            "vulnerability_matches": len(matches),
        }
    (output / "evidence.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence, indent=2))
    return vulnerable


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(int(audit(args.image, args.output)))
