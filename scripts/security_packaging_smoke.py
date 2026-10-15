"""Exercise real signature verification in a frozen binary without installing."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.capture_release_updates import captured_payload, file_digest
from scripts.sidecar_pkg.update_limits import MAX_ARCHIVE_BYTES


def smoke(binary: Path, payload: Path, bundle: Path, report: Path) -> None:
    evidence: dict[str, Any] = {
        "passed": False,
        "binary_sha256": file_digest(binary),
        "payload": payload.name,
        "payload_sha256": file_digest(payload),
        "bundle_sha256": file_digest(bundle),
        "verifier_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=10
        ).strip(),
        "cases": {},
    }
    try:
        with tempfile.TemporaryDirectory(prefix="runway-packaging-") as temp:
            directory = Path(temp)
            env = {**os.environ, "RUNWAY_CONFIG_DIR": temp}
            if sys.platform == "darwin":
                # Exercise the packaged default, not a developer's CA override
                # or existing Sigstore cache. Never change the host trust store.
                home = directory / "home"
                home.mkdir()
                env["HOME"] = str(home)
                for variable in (
                    "SSL_CERT_FILE",
                    "SSL_CERT_DIR",
                    "REQUESTS_CA_BUNDLE",
                    "CURL_CA_BUNDLE",
                ):
                    env.pop(variable, None)
            tampered = directory / "tampered.txt"
            tampered.write_text("tampered update")
            malformed = directory / "malformed.json"
            malformed.write_text("{invalid bundle")
            oversized = directory / "oversized.zip"
            with oversized.open("wb") as fh:
                fh.truncate(MAX_ARCHIVE_BYTES + 1)
            cases = (
                ("valid", payload, bundle, 0, ""),
                ("tampered", tampered, bundle, 1, "Update signature verification failed"),
                (
                    "missing-bundle",
                    payload,
                    directory / "missing.json",
                    1,
                    "Update signature verification failed",
                ),
                ("malformed-bundle", payload, malformed, 1, "Update signature verification failed"),
                ("oversized", oversized, bundle, 1, "Update archive exceeds size limit"),
            )
            for name, archive, verification_bundle, expected, diagnostic in cases:
                result = subprocess.run(
                    [
                        str(binary.resolve()),
                        "--verify-update",
                        str(archive.resolve()),
                        "--bundle",
                        str(verification_bundle.resolve()),
                    ],
                    env=env,
                    check=False,
                    timeout=120,
                    capture_output=True,
                    text=True,
                )
                passed = result.returncode == expected and diagnostic in result.stderr
                evidence["cases"][name] = {
                    "passed": passed,
                    "exit_code": result.returncode,
                    "diagnostic": result.stderr,
                }
                if not passed:
                    raise RuntimeError(f"Frozen verifier failed {name} acceptance")
            evidence["passed"] = True
    finally:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(evidence, indent=2) + "\n")
    print("Frozen verifier accepted the signed payload and rejected all refusal cases")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--release-dir", type=Path)
    parser.add_argument("--platform", choices=["Linux-CLI", "Linux", "Windows", "macOS"])
    parser.add_argument("--report", type=Path, default=Path("frozen-verifier-evidence.json"))
    args = parser.parse_args()
    if args.release_dir or args.platform:
        if not (args.release_dir and args.platform) or args.payload or args.bundle:
            parser.error("Provide release-dir/platform together, without payload/bundle")
        payload, bundle = captured_payload(args.release_dir, args.platform)
    elif args.payload or args.bundle:
        if not (args.payload and args.bundle):
            parser.error("Provide payload/bundle together")
        payload, bundle = args.payload, args.bundle
    else:
        fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/sigstore"
        payload, bundle = fixtures / "SHA256SUMS.txt", fixtures / "SHA256SUMS.txt.sigstore.json"
    smoke(args.binary, payload, bundle, args.report)


if __name__ == "__main__":
    main()
