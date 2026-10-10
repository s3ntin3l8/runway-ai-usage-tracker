"""Exercise real signature verification in a frozen binary without installing."""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--binary", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
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
print("Frozen verifier accepted the signed fixture and rejected tampering")
