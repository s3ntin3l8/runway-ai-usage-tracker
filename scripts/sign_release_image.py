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

One caveat, because the pinned cosign v3.1.3 does not check the token
endpoint's HTTP status before decoding the body: a plain-text rejection
reaches cosign as a JSON decode error and is indistinguishable from the
transient fault, so it is retried before the step fails. It still fails,
and nothing is promoted; only the failure is slower to arrive. cosign
v3.1.4 checks the status, and this script already treats a stated
rejection as permanent, so moving the pin forward tightens the retry
without a code change.

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

# A rejection the token endpoint states outright is permanent and must never
# be retried, whatever else the message contains.
#
# cosign v3.1.3 - the version this repository pins - does not check the
# token endpoint's HTTP status before decoding the body, so a plain-text 403
# reaches cosign as a JSON decode error and looks exactly like the transient
# fault. The two are indistinguishable in v3.1.3's output. cosign v3.1.4 adds
# the status check, and then a rejection is reported as such:
#
#     identity token request to provider github-actions failed with status
#     403 Forbidden
#
# Treating that as permanent is what keeps a real misconfiguration from
# being retried once the pinned cosign is moved forward. The marker is
# checked first so it wins over the decode-error markers above: a v3.1.4
# rejection whose body also happens to be unparseable carries both.
_PERMANENT_MARKERS = (
    "failed with status 403",
    "failed with status 401",
)

# Four attempts with an escalating ten-second backoff: about 60 seconds of
# waiting in total, matching the bound the issue asks for and the pattern in
# .github/actions/registry-login/login.sh. The bound is tight on purpose - a
# generous one would mask a genuine auth misconfiguration behind a slow
# failure.
_ATTEMPTS = 4
_BACKOFF_SECONDS = 10

# Per-attempt ceiling on the cosign process itself. A real signing takes
# about three seconds (measured on the run that signed successfully), so 60
# seconds is generous for the work and still bounds the whole retry inside
# the enclosing job's timeout:
#
#     4 x 60 s attempts + 60 s of backoff = 300 s
#
# The `merge` job allows 15 minutes (900 s) and the steps ahead of the
# signing - checkout, artifact download, buildx setup, cosign install,
# registry login, and the two-architecture audit - measured about 132 s. A
# 300 s signing step plus that overhead leaves the job in control of its own
# timeout, so a hung cosign is reported as the script's annotated failure
# instead of being cancelled by the platform with no explanation.
_ATTEMPT_TIMEOUT_SECONDS = 60


def is_transient(stderr: str) -> bool:
    """Whether this failure is a transient OIDC token fault worth retrying."""
    if any(marker in stderr for marker in _PERMANENT_MARKERS):
        return False
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
                timeout=_ATTEMPT_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("cosign is not installed; the image was not signed") from exc
        except subprocess.TimeoutExpired as exc:
            # The process was killed mid-sign, so the signature may or may not
            # have been written. Fail closed either way - the digest is
            # re-signed cleanly on the next attempt.
            raise RuntimeError(
                f"{reference}: signing timed out after {exc.timeout}s; "
                "no public tags were promoted."
            ) from exc
        # Keep cosign's own output in the job log: it is the only diagnostic a
        # failed signing attempt leaves behind.
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode == 0:
            return
        if not is_transient(result.stderr) or attempt == _ATTEMPTS:
            raise RuntimeError(
                f"{reference}: signing failed; no public tags were promoted. "
                "Re-run the workflow to rebuild, re-audit, and sign before "
                "promotion. The audit candidate is retained for diagnosis.\n"
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
