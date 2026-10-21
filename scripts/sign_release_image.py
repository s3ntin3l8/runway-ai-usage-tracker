"""Sign a release image digest, retrying only a transient OIDC token fault.

The release signer publishes the signature that the promoted tags are
verified against, so this must fail closed: the caller promotes nothing
unless the signature was actually written. A retry therefore changes
*when* this fails, never *whether* it fails.

Only one failure is retried: a malformed response from the ambient GitHub
OIDC token endpoint, which surfaces as a JSON decode error inside cosign's
ambient credential provider:

    retrieving ID token: reading ID token: fetching ambient OIDC
    credentials: invalid character 'u' looking for beginning of value

That is a transient fault of the token endpoint, not a problem with the
image, the registry, or this repository's credentials. Every other failure
- a rejected identity, a missing `id-token: write` permission, a digest
the registry does not have - is permanent and is reported immediately, so a
real misconfiguration surfaces in one attempt instead of four.

The retry mirrors ``scripts/sign_release_assets.py``, which retries
Rekor's equivalent-entry conflict and nothing else.
"""

from __future__ import annotations

import subprocess
import sys
import time

# A transient fault is the ambient credential provider failing to decode the
# token endpoint's answer: cosign wraps it as "fetching ambient OIDC
# credentials: invalid character 'u' looking for beginning of value". Both
# markers must be present, so a rejected identity, a missing
# `id-token: write` permission or an unknown digest - which fail with
# entirely different messages - is never retried.
_TRANSIENT_MARKERS = (
    "fetching ambient OIDC credentials",
    "invalid character",
)

# Four attempts with an escalating ten-second backoff: about 60 seconds of
# waiting in total, matching the bound the issue asks for and the pattern in
# .github/actions/registry-login/login.sh. The bound is tight on purpose - a
# generous one would mask a genuine auth misconfiguration behind a slow
# failure.
_ATTEMPTS = 4
_BACKOFF_SECONDS = 10


def is_transient(stderr: str) -> bool:
    """Whether this failure is a transient OIDC token fault worth retrying."""
    return all(marker in stderr for marker in _TRANSIENT_MARKERS)


def sign_image(reference: str) -> None:
    """Sign ``reference`` (``registry/name@digest``), retrying a token fault.

    Raises ``RuntimeError`` when the image cannot be signed, so the caller
    promotes no tags.
    """
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            result = subprocess.run(
                ["cosign", "sign", "--yes", reference],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("cosign is not installed; the image was not signed") from exc
        # Keep cosign's own output in the job log: it is the only diagnostic a
        # failed signing attempt leaves behind.
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode == 0:
            return
        if not is_transient(result.stderr) or attempt == _ATTEMPTS:
            raise RuntimeError(
                f"{reference}: signing failed; no public tags were promoted.\n"
                f"{result.stderr.strip()}"
            )
        print(
            f"::warning::Ambient OIDC token fault on attempt {attempt}/{_ATTEMPTS}; retrying.",
            file=sys.stderr,
        )
        time.sleep(_BACKOFF_SECONDS * attempt)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m scripts.sign_release_image <registry/name@digest>")
    try:
        sign_image(sys.argv[1])
    except RuntimeError as exc:
        # The same annotation the inline step emitted, so a failed signing
        # attempt is still obvious in the job log.
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
