"""Sign release assets, retrying only Rekor's equivalent-entry conflict.

Each retry launches a new cosign process (fresh ephemeral signing key). A failed
or partial bundle never survives; the workflow must independently verify every
successful bundle before uploading the release set.
"""

import subprocess
import sys
import time
from pathlib import Path


def sign_asset(asset: Path) -> None:
    bundle = Path(f"{asset}.sigstore.json")
    for attempt in range(3):
        bundle.unlink(missing_ok=True)
        try:
            result = subprocess.run(
                ["cosign", "sign-blob", "--yes", "--bundle", str(bundle), str(asset)],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            bundle.unlink(missing_ok=True)
            raise
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode == 0 and bundle.is_file():
            return
        bundle.unlink(missing_ok=True)
        conflict = "[409]" in result.stderr and "createLogEntryConflict" in result.stderr
        if not conflict or attempt == 2:
            raise RuntimeError(f"{asset.name}: signing failed; release set withheld")
        print("::warning::Rekor equivalent-entry conflict; retrying with a fresh signing key.")
        time.sleep(5)


def main() -> None:
    root = Path(sys.argv[1])
    assets = sorted(
        p
        for p in root.glob("Runway-Sidecar-*")
        if p.name.endswith((".dmg", ".exe", ".zip", ".tar.gz"))
    )
    if not assets:
        raise RuntimeError("No sidecar assets to sign")
    for asset in [*assets, root / "SHA256SUMS.txt"]:
        sign_asset(asset)


if __name__ == "__main__":
    main()
