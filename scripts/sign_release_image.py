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
and nothing is promoted; only the failure is slower to arrive.

cosign v3.1.4 checks the status first and reports it, and this script
classifies by that status when one is present: a 5xx or a 429 is retried,
any other status is not. So moving the pin forward makes the retry both
narrower and still effective, with no change here.

The retry mirrors ``scripts/sign_release_assets.py``, which retries
Rekor's equivalent-entry conflict and nothing else.
"""

from __future__ import annotations

import re
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

# cosign reports the token endpoint's HTTP status in this shape:
#
#     identity token request to provider github-actions failed with status
#     502 Bad Gateway
#
# cosign v3.1.3 - the version this repository pins - does not report it at
# all, because it decodes the body without checking the status first. The
# status is therefore optional in a message, and its presence is what
# separates the two failure kinds:
#
#   * a status of 5xx means the endpoint is unwell, which is the transient
#     fault worth retrying;
#   * a status of 4xx means the endpoint refused the request - a rejected
#     identity, a missing `id-token: write` permission - which a retry
#     cannot fix;
#   * no status at all means cosign could not decode the body, which is
#     what v3.1.3 reports for every non-JSON answer.
#
# Classifying by status rather than by the decode error is what keeps the
# retry firing when the pinned cosign moves to v3.1.4 or later: those
# versions report a 5xx as "failed with status 502", with no "invalid
# character" anywhere in the message.
_STATUS_RE = re.compile(r"failed with status (\d{3})")

# Statuses worth retrying: the endpoint is unwell (5xx) or is rate limiting
# (429). Every other status is the endpoint refusing the request, which a
# retry does not fix.
_TRANSIENT_STATUSES = frozenset({429})

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


def _as_command_data(message: str) -> str:
    """``message`` escaped for use as a workflow command's data.

    A GitHub Actions workflow command is terminated by the newline, so the
    data has to be a single physical line. GitHub's documented escaping
    keeps the content intact: ``%0A`` for a newline, ``%0D`` for a carriage
    return and ``%25`` for a percent sign. The runner decodes them when it
    renders the annotation.

    The percent sign is escaped first, because the escape sequences
    themselves contain one - otherwise ``%0A`` in the original message would
    be decoded as a newline.
    """
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def is_transient(stderr: str) -> bool:
    """Whether this failure is a transient OIDC token fault worth retrying."""
    status = _STATUS_RE.search(stderr)
    if status is not None:
        # The endpoint answered, so its status decides: an unwell or
        # rate-limiting endpoint is worth another attempt, a refusal is
        # not. This wins over the decode markers below, because a
        # rejection whose body also failed to parse carries both.
        code = int(status.group(1))
        return code >= 500 or code in _TRANSIENT_STATUSES
    # No status: cosign could not decode the body. That is the observed
    # transient fault - and, under cosign v3.1.3, also what a plain-text
    # rejection looks like.
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
                # cosign's output is not guaranteed to be UTF-8, and a
                # decode error would escape this function uncaught - the step
                # would die on a traceback instead of the annotated failure
                # it is supposed to report. Replace what cannot be decoded;
                # the text is only used for the log and the failure marker.
                errors="replace",
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
        # flush: stdout is block-buffered when it is not a tty, and the
        # runner does not set PYTHONUNBUFFERED. Without the flush the
        # warning only reaches the log when the process exits, so an
        # operator watching a slow release sees nothing while it retries.
        print(
            f"::warning::Ambient OIDC token fault on attempt {attempt}/{_ATTEMPTS}; retrying.",
            flush=True,
        )
        time.sleep(_BACKOFF_SECONDS * attempt)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m scripts.sign_release_image <registry/name@digest>")
    try:
        sign_image(sys.argv[1])
    except RuntimeError as exc:
        # The same annotation the inline step emitted, so a failed signing
        # attempt is still obvious in the job log. It goes to stdout, like
        # the repository's other workflow-command emitters.
        #
        # A workflow command ends at the newline, so the message is escaped
        # into a single physical line. The whole diagnostic survives, and
        # the runner renders the escaped newlines as they were.
        print(f"::error::{_as_command_data(str(exc))}", flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
