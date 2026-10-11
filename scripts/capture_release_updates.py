"""Capture a versioned release and verify its portable updater bytes without installing."""

import argparse
import hashlib
import json
import re
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.signatures import verify_update
from scripts.sidecar_pkg.update_limits import (
    MAX_ARCHIVE_BYTES,
    MAX_BUNDLE_BYTES,
    MAX_CHECKSUM_BYTES,
    MAX_METADATA_BYTES,
    bounded_chunks,
    bounded_read,
)

REPOSITORY = "s3ntin3l8/runway-ai-usage-tracker"
PLATFORMS = {"Linux-CLI": "tar.gz", "Linux": "tar.gz", "Windows": "zip", "macOS": "zip"}


def file_digest(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def api(path: str) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        headers={
            "User-Agent": "Runway-release-acceptance",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(bounded_read(response, MAX_METADATA_BYTES))


def release_assets(metadata: dict[str, Any], tag: str) -> dict[str, dict[str, Any]]:
    if metadata["tag_name"] != tag or metadata["draft"]:
        raise ValueError("Expected a published versioned release")
    names = {
        f"Runway-Sidecar-{platform}-{tag}.{extension}{suffix}"
        for platform, extension in PLATFORMS.items()
        for suffix in ("", ".sha256", ".sigstore.json")
    }
    assets = {}
    for asset in metadata["assets"]:
        name = asset["name"]
        if name in names:
            if name in assets:
                raise ValueError("Duplicate release asset")
            assets[name] = {key: asset.get(key) for key in ("id", "size", "digest", "updated_at")}
    if assets.keys() != names:
        raise ValueError("Release is missing portable archives, checksums, or bundles")
    return assets


def release_commit(tag: str) -> str:
    obj = api(f"git/ref/tags/{tag}")["object"]
    for _ in range(5):
        if obj["type"] == "commit" and re.fullmatch(r"[0-9a-f]{40}", obj["sha"]):
            return str(obj["sha"])
        if obj["type"] != "tag":
            break
        obj = api(f"git/tags/{obj['sha']}")["object"]
    raise ValueError("Release tag does not resolve to a commit")


def download(tag: str, name: str, output: Path) -> None:
    limit = (
        MAX_BUNDLE_BYTES
        if name.endswith(".sigstore.json")
        else MAX_CHECKSUM_BYTES
        if name.endswith(".sha256")
        else MAX_ARCHIVE_BYTES
    )
    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    fh = output.open("xb")
    try:
        with fh, urllib.request.urlopen(url, timeout=60) as response:
            for chunk in bounded_chunks(response, limit):
                fh.write(chunk)
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def capture(tag: str, output: Path) -> dict[str, Any]:
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", tag):
        raise ValueError("Expected a versioned release tag, never rolling edge")
    metadata = api(f"releases/tags/{tag}")
    assets = release_assets(metadata, tag)
    commit = release_commit(tag)
    output.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "tag": tag,
        "release_id": metadata["id"],
        "source_commit": commit,
        "captured_at": datetime.now(UTC).isoformat(),
        "verifier_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=10
        ).strip(),
        "passed": False,
        "assets": {},
    }
    try:
        for name, info in sorted(assets.items()):
            path = output / name
            download(tag, name, path)
            digest = file_digest(path)
            if path.stat().st_size != info["size"]:
                raise ValueError("Downloaded release asset size changed")
            if info["digest"] and info["digest"] != f"sha256:{digest}":
                raise ValueError("Downloaded release asset digest changed")
            manifest["assets"][name] = {**info, "sha256": digest}
        for platform, extension in PLATFORMS.items():
            name = f"Runway-Sidecar-{platform}-{tag}.{extension}"
            checksum = (output / f"{name}.sha256").read_text().split()
            if checksum != [manifest["assets"][name]["sha256"], name]:
                raise ValueError("Release checksum does not match the captured payload")
            verify_update(output / name, output / f"{name}.sigstore.json")
        after = api(f"releases/tags/{tag}")
        if (
            after["id"] != metadata["id"]
            or release_assets(after, tag) != assets
            or release_commit(tag) != commit
        ):
            raise ValueError("Release changed during capture")
        manifest["passed"] = True
    finally:
        (output / "capture.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def captured_payload(directory: Path, platform: str) -> tuple[Path, Path]:
    manifest = json.loads((directory / "capture.json").read_text())
    tag = manifest["tag"]
    if (
        manifest.get("schema_version") != 1
        or manifest.get("repository") != REPOSITORY
        or manifest.get("passed") is not True
        or not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", tag)
    ):
        raise ValueError("Invalid or failed release capture")
    names = {
        f"Runway-Sidecar-{p}-{tag}.{extension}{suffix}"
        for p, extension in PLATFORMS.items()
        for suffix in ("", ".sha256", ".sigstore.json")
    }
    if manifest["assets"].keys() != names:
        raise ValueError("Incomplete release capture")
    for name in names:
        path = directory / name
        entry = manifest["assets"][name]
        if path.stat().st_size != entry["size"] or file_digest(path) != entry["sha256"]:
            raise ValueError("Captured asset integrity mismatch")
    name = f"Runway-Sidecar-{platform}-{tag}.{PLATFORMS[platform]}"
    return directory / name, directory / f"{name}.sigstore.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    capture(args.release_tag, args.output)
