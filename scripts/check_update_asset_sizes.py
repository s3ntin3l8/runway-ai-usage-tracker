"""Fail packaging if a supported update payload exceeds the updater budget."""

import argparse
from pathlib import Path

from scripts.sidecar_pkg.update_limits import MAX_ARCHIVE_BYTES, MAX_BUNDLE_BYTES


def check_sizes(directory: Path) -> list[str]:
    failures = []
    payloads = [
        p for p in directory.glob("Runway-Sidecar-*") if p.name.endswith((".zip", ".tar.gz"))
    ]
    if not payloads:
        failures.append("No portable update payloads found")
    for path in payloads:
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            failures.append(f"{path.name} exceeds 256 MiB archive budget")
        bundle = path.with_name(path.name + ".sigstore.json")
        if not bundle.is_file() or bundle.stat().st_size > MAX_BUNDLE_BYTES:
            failures.append(f"{bundle.name} is missing or exceeds 1 MiB bundle budget")
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    issues = check_sizes(args.directory)
    for issue in issues:
        print(issue)
    raise SystemExit(bool(issues))
