"""Catalog immutable image bytes and enforce Runway's release vulnerability policy."""

import argparse
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Any

from scripts.image_policy import check_database, database_checksum, evaluate, load_exceptions

SYFT = "anchore/syft@sha256:3eb5379ba7b409c3f4069b686110527af0c47df993fa5c10d13e7cf34f49b1aa"
GRYPE = "anchore/grype@sha256:e4a44ef45d285b829ce6efe2642980329661bd2d18eab5fc539138d4adaebbbe"
IMAGE_PATTERN = r"ghcr\.io/s3ntin3l8/runway@sha256:[0-9a-f]{64}"
DEFAULT_EXCEPTIONS = Path(__file__).resolve().parents[1] / "docs/security/image-exceptions.json"
# Upstream can publish a new release while the database is being downloaded.
# Every attempt re-downloads the whole database, so the attempts are few and
# the backoff short; three attempts stay far inside the workflow job timeout.
DB_UPDATE_ATTEMPTS = 3
DB_UPDATE_BACKOFF_SECONDS = 10


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


def read_listing(path: Path) -> dict[str, Any] | None:
    """Return the release upstream advertises, or None when no listing was recorded."""
    if not path.exists():
        return None
    entries = json.loads(path.read_text())
    if not isinstance(entries, list) or not entries:
        raise ValueError("Invalid upstream vulnerability database listing")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("status") == "active":
            return entry
    raise ValueError("Invalid upstream vulnerability database listing")


def prepare_database(cache: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Download the release upstream currently advertises and return it with its listing.

    The advertised release is returned with the database so the audit can
    verify that the scan used it. The listing is taken when the database is
    prepared: upstream publishing again mid-audit does not invalidate a scan
    whose database was the current release when the audit started.
    """
    cache.mkdir(parents=True, exist_ok=True)
    status_path = cache / "status.json"
    listing_path = cache / "listing.json"
    for attempt in range(DB_UPDATE_ATTEMPTS):
        scanner(cache, "db", "update")
        with status_path.open("w") as fh:
            scanner(cache, "db", "status", "-o", "json", stdout=fh)
        with listing_path.open("w") as fh:
            scanner(cache, "db", "list", "-o", "json", stdout=fh)
        status = json.loads(status_path.read_text())
        listing = read_listing(listing_path)
        if listing is None:
            raise ValueError("Missing upstream vulnerability database listing")
        if database_checksum(status) == listing.get("checksum"):
            return status, listing
        if attempt + 1 < DB_UPDATE_ATTEMPTS:
            sleep(DB_UPDATE_BACKOFF_SECONDS)
    raise ValueError("Vulnerability database is not the release upstream currently advertises")


def scan_archive(
    archive: Path,
    arch: str,
    output: Path,
    cache: Path,
    expected_checksum: str | None = None,
) -> dict[str, Any]:
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
    check_database(report["descriptor"]["db"]["status"], datetime.now(UTC), expected_checksum)
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
        # Entries whose review window closed. They waive nothing, so the
        # findings they covered are blocked; naming them here is what turns
        # "the document is invalid" into "this advisory lost its exception".
        "expired_exceptions": [],
    }
    try:
        exceptions = load_exceptions(exceptions_path, datetime.now(UTC))
        evidence["expired_exceptions"] = [
            {
                "id": entry["id"],
                "package": entry["package"],
                "version": entry["version"],
                "architecture": entry["architecture"],
                "expires_at": entry["expires_at"],
            }
            for entry in exceptions.expired.values()
        ]
        with tempfile.TemporaryDirectory(prefix="runway-image-") as temp:
            temp_path = Path(temp)
            cache = database or temp_path / "database"
            if database:
                db = json.loads((cache / "status.json").read_text())
                # An operator cache prepared before listing records existed is
                # checked for age only.
                listing = read_listing(cache / "listing.json")
            else:
                db, listing = prepare_database(cache)
            expected_checksum = listing["checksum"] if listing else None
            # Keep public feed evidence even when validation rejects it. Failed
            # audits must distinguish a stale advertised release from a stale cache.
            evidence["database"] = db
            evidence["database_listing"] = listing
            check_database(db, datetime.now(UTC), expected_checksum)
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
                report = scan_archive(local_archive, arch, output, cache, expected_checksum)
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
                "expired_exceptions": len(evidence["expired_exceptions"]),
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
