"""Verify updater payloads against the repository's Sigstore signing identity."""

import hashlib
import sys
from pathlib import Path

from scripts.sidecar_pkg.update_limits import MAX_ARCHIVE_BYTES, MAX_BUNDLE_BYTES, UpdateSizeError

SIGNING_IDENTITY = "https://github.com/s3ntin3l8/runway-ai-usage-tracker/.github/workflows/sidecar-build.yml@refs/heads/main"
SIGNING_ISSUER = "https://token.actions.githubusercontent.com"


class UpdateVerificationError(Exception):
    """No extraction or installation may follow this failure."""


def verification_command(argv: list[str]) -> bool:
    """Read-only verification diagnostic, usable by frozen packaging checks."""
    if not argv or argv[0] != "--verify-update":
        return False
    import argparse

    parser = argparse.ArgumentParser(description="Verify an update without installing it")
    parser.add_argument("--verify-update", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verify_update(args.verify_update, args.bundle)
    except UpdateVerificationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(0)


def verify_update(archive: Path, bundle_path: Path) -> None:
    """Validate bytes, identity, chain, and transparency evidence, fail closed.

    Imports are lazy: source-only sidecars never need the verification extras.
    Production trust metadata is refreshed by the official client's TUF path.
    No operator insecure-TLS setting is passed into this verifier.
    """
    try:
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise UpdateSizeError("Update archive exceeds size limit")
        from sigstore.hashes import Hashed
        from sigstore.models import Bundle
        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity
        from sigstore_models.common.v1 import HashAlgorithm

        with bundle_path.open("rb") as fh:
            bundle_bytes = fh.read(MAX_BUNDLE_BYTES + 1)
        if len(bundle_bytes) > MAX_BUNDLE_BYTES:
            raise UpdateSizeError("Update bundle exceeds size limit")
        digest = hashlib.sha256()
        with archive.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        Verifier.production().verify_artifact(
            Hashed(algorithm=HashAlgorithm.SHA2_256, digest=digest.digest()),
            Bundle.from_json(bundle_bytes),
            Identity(identity=SIGNING_IDENTITY, issuer=SIGNING_ISSUER),
        )
    except UpdateSizeError as exc:
        raise UpdateVerificationError(str(exc)) from exc
    except Exception as exc:
        # SDK exceptions may include bundle contents or network details; expose
        # only a stable failure, never fall back to checksums or manual crypto.
        raise UpdateVerificationError("Update signature verification failed") from exc
