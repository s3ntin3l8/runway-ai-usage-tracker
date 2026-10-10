"""Exercise real signature verification in a frozen binary without installing."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--binary", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
fixtures = root / "tests/fixtures/sigstore"
with tempfile.TemporaryDirectory(prefix="runway-packaging-") as temp:
    env = os.environ.copy()
    env["RUNWAY_CONFIG_DIR"] = temp
    command = [
        str(args.binary.resolve()),
        "--verify-update",
        str(fixtures / "SHA256SUMS.txt"),
        "--bundle",
        str(fixtures / "SHA256SUMS.txt.sigstore.json"),
    ]
    subprocess.run(command, env=env, check=True, timeout=120)
    tampered = Path(temp) / "tampered.txt"
    tampered.write_text("tampered update")
    command[2] = str(tampered)
    result = subprocess.run(command, env=env, check=False, timeout=120)
    if result.returncode == 0:
        raise SystemExit("Frozen verifier accepted a tampered payload")
    # Sparse fixture exercises the real cap without allocating 256 MiB of RAM.
    from scripts.sidecar_pkg.update_limits import MAX_ARCHIVE_BYTES

    oversized = Path(temp) / "oversized.zip"
    with oversized.open("wb") as fh:
        fh.truncate(MAX_ARCHIVE_BYTES + 1)
    command[2] = str(oversized)
    oversized_result = subprocess.run(
        command, env=env, check=False, timeout=120, capture_output=True, text=True
    )
    if (
        oversized_result.returncode != 1
        or "Update archive exceeds size limit" not in oversized_result.stderr
    ):
        raise SystemExit("Frozen verifier did not enforce the local archive size limit")
print("Frozen verifier accepted the signed fixture and rejected tampering")
