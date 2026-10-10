"""Catalog immutable image bytes and enforce Runway's release vulnerability policy."""

import argparse
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.image_policy import check_database, evaluate, load_exceptions

SYFT = "anchore/syft@sha256:3eb5379ba7b409c3f4069b686110527af0c47df993fa5c10d13e7cf34f49b1aa"
GRYPE = "anchore/grype@sha256:e4a44ef45d285b829ce6efe2642980329661bd2d18eab5fc539138d4adaebbbe"
IMAGE_PATTERN = r"ghcr\.io/s3ntin3l8/runway@sha256:[0-9a-f]{64}"
DEFAULT_EXCEPTIONS = Path(__file__).resolve().parents[1] / "docs/security/image-exceptions.json"


def run(*args: str, stdout: Any = None) -> None:
    subprocess.run(list(args), stdout=stdout, check=True, timeout=600)


def scanner(cache: Path, *args: str, stdout: Any = None) -> None:
    run(
        "docker",
        "run",
        "--rm",
        "--tmpfs",
        "/tmp:rw,mode=1777",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "GRYPE_DB_CACHE_DIR=/cache",
        "-e",
        "GRYPE_DB_AUTO_UPDATE=false",
        "-v",
        f"{cache.resolve()}:/cache",
        GRYPE,
        *args,
        stdout=stdout,
    )


def prepare_database(cache: Path) -> dict[str, Any]:
    cache.mkdir(parents=True, exist_ok=True)
    scanner(cache, "db", "update")
    status = cache / "status.json"
    with status.open("w") as fh:
        scanner(cache, "db", "status", "-o", "json", stdout=fh)
    return json.loads(status.read_text())


def scan_archive(archive: Path, arch: str, output: Path, cache: Path) -> dict[str, Any]:
    sbom_path = output / f"{arch}.cyclonedx.json"
    with sbom_path.open("w") as fh:
        run(
            "docker",
            "run",
            "--rm",
            "-v",
            f"{archive.resolve()}:/image.tar:ro",
            SYFT,
            "docker-archive:/image.tar",
            "-o",
            "cyclonedx-json",
            stdout=fh,
        )
    sbom = json.loads(sbom_path.read_text())
    inventory = [
        {"name": c["name"], "version": c.get("version"), "purl": c.get("purl")}
        for c in sbom["components"]
    ]
    if not inventory:
        raise ValueError("Scanner produced an empty package inventory")
    (output / f"{arch}.packages.json").write_text(json.dumps(inventory, indent=2))
    scan_path = output / f"{arch}.vulnerabilities.json"
    with scan_path.open("w") as fh:
        run(
            "docker",
            "run",
            "--rm",
            "--tmpfs",
            "/tmp:rw,mode=1777",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "GRYPE_DB_CACHE_DIR=/cache",
            "-e",
            "GRYPE_DB_AUTO_UPDATE=false",
            "-v",
            f"{cache.resolve()}:/cache:ro",
            "-v",
            f"{sbom_path.resolve()}:/input.json:ro",
            GRYPE,
            "sbom:/input.json",
            "-o",
            "json",
            stdout=fh,
        )
    report = json.loads(scan_path.read_text())
    # The scanner must have used the same database we retained and validated.
    check_database(report["descriptor"]["db"]["status"], datetime.now(UTC))
    if (
        report["descriptor"]["db"]["status"]["from"]
        != json.loads((cache / "status.json").read_text())["from"]
    ):
        raise ValueError("Scanner database changed during audit")
    return report


def executable_manifests(index: dict[str, Any]) -> dict[str, Any]:
    manifests = {}
    for manifest in index.get("manifests", []):
        platform = manifest.get("platform", {})
        if (
            platform == {"architecture": "unknown", "os": "unknown"}
            and manifest.get("annotations", {}).get("vnd.docker.reference.type")
            == "attestation-manifest"
        ):
            continue
        arch = platform.get("architecture")
        if platform.get("os") != "linux" or arch not in {"amd64", "arm64"} or arch in manifests:
            raise ValueError("Unexpected or duplicate executable image platform")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("digest", "")):
            raise ValueError("Invalid architecture manifest digest")
        manifests[arch] = manifest
    if set(manifests) != {"amd64", "arm64"}:
        raise ValueError("Image requires both linux/amd64 and linux/arm64")
    return manifests


def audit(
    image: str | None,
    output: Path,
    *,
    archive: Path | None = None,
    architecture: str | None = None,
    database: Path | None = None,
    exceptions_path: Path = DEFAULT_EXCEPTIONS,
) -> bool:
    output.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, Any] = {
        "image": image,
        "scanned_at": datetime.now(UTC).isoformat(),
        "syft": SYFT,
        "grype": GRYPE,
        "platforms": {},
        "passed": False,
    }
    try:
        exceptions = load_exceptions(exceptions_path, datetime.now(UTC))
        with tempfile.TemporaryDirectory(prefix="runway-image-") as temp:
            temp_path = Path(temp)
            cache = database or temp_path / "database"
            if database:
                db = json.loads((cache / "status.json").read_text())
            else:
                db = prepare_database(cache)
            check_database(db, datetime.now(UTC))
            evidence["database"] = db
            if archive:
                if architecture not in {"amd64", "arm64"}:
                    raise ValueError("Local archives require --architecture amd64 or arm64")
                targets = {architecture: None}
            else:
                if image is None or not re.fullmatch(IMAGE_PATTERN, image):
                    raise ValueError("Use an immutable Runway image index digest")
                raw = json.loads(
                    subprocess.check_output(
                        ["docker", "buildx", "imagetools", "inspect", "--raw", image],
                        text=True,
                        timeout=60,
                    )
                )
                manifests = executable_manifests(raw)
                targets = {
                    arch: image.split("@", maxsplit=1)[0] + "@" + manifests[arch]["digest"]
                    for arch in ("amd64", "arm64")
                }
            for arch, target in targets.items():
                local_archive = archive or temp_path / f"{arch}.tar"
                if target:
                    run("docker", "pull", "--platform", f"linux/{arch}", target)
                    run("docker", "image", "save", "--output", str(local_archive), target)
                report = scan_archive(local_archive, arch, output, cache)
                policy = evaluate(report, arch, exceptions)
                policy["image"] = target
                (output / f"{arch}.policy.json").write_text(json.dumps(policy, indent=2))
                evidence["platforms"][arch] = policy
            evidence["passed"] = all(p["passed"] for p in evidence["platforms"].values())
    except Exception as exc:
        evidence["error"] = str(exc)
        raise
    finally:
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2))
    print(
        json.dumps(
            {
                "passed": evidence["passed"],
                "counts": {arch: p["counts"] for arch, p in evidence["platforms"].items()},
            },
            indent=2,
        )
    )
    return not evidence["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--architecture", choices=["amd64", "arm64"])
    parser.add_argument("--database", type=Path, help="Reuse a previously prepared database cache")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    args = parser.parse_args()
    if bool(args.image) == bool(args.archive):
        parser.error("Provide exactly one image digest or --archive")
    raise SystemExit(
        int(
            audit(
                args.image,
                args.output,
                archive=args.archive,
                architecture=args.architecture,
                database=args.database,
                exceptions_path=args.exceptions,
            )
        )
    )
