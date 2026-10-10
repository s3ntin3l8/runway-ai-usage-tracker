"""Verify every portable release payload using the production updater policy."""

import argparse
from pathlib import Path

from scripts.sidecar_pkg.signatures import UpdateVerificationError, verify_update


def verify_release(directory: Path) -> list[str]:
    payloads = sorted(
        p for p in directory.glob("Runway-Sidecar-*") if p.name.endswith((".zip", ".tar.gz"))
    )
    if not payloads:
        return ["No portable update payloads found"]
    failures = []
    for payload in payloads:
        try:
            verify_update(payload, payload.with_name(payload.name + ".sigstore.json"))
        except UpdateVerificationError as exc:
            failures.append(f"{payload.name}: {exc}")
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    failures = verify_release(parser.parse_args().directory)
    for failure in failures:
        print(failure)
    raise SystemExit(bool(failures))
