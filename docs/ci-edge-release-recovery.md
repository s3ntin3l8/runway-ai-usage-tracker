# Edge release CI failures after #674 and #680

## Observed failures

Both merged revisions passed their `CI/CD` runs. The failures were in the
separate `Sidecar Edge Build` workflow, which runs only after a relevant push to
`main` (or explicit dispatch).

| Revision | Native build/release evidence | Outcome |
| --- | --- | --- |
| `02ddff13` (#674) | [Run 39223996307, attempt 1](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/39223996307/attempts/1): Chocolatey returned HTTP 503 for NSIS 3.13.0; `makensis` was then unavailable | Windows installer failed; other platforms, attestation and partial edge publication succeeded. Attempt 2 was subsequently cancelled. |
| `cb0002d1` (#680) | [Run 39312488123, attempt 1](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/39312488123/attempts/1): every platform built successfully; macOS ZIP signing returned Rekor HTTP 409 `createLogEntryConflict` | Attestation failed. The caller then attempted to download the absent signed asset set and failed again. |

These logs identify the failing external operations, not an application test or
Claude credential regression. They do not establish why Rekor already had an
equivalent entry, or whether its first submission completed before a response
was lost. No collision mechanism is claimed as proven.

[Attempt 2 of run 39312488123](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/39312488123/attempts/2)
reran the failed jobs at the unchanged `cb0002d1` revision. Signing, independent
verification and edge publication all succeeded. This establishes recovery of
that external operation without an application-code change; it does not test
the new automatic retry path.

## Why the pull request checks missed them

The PR frozen-verifier matrix built and executed all four platform binaries,
including Windows, but did not provision NSIS or build the Windows installer.
The separate Linux NSIS check compiled the installer with a dummy executable;
it could not exercise the Windows Chocolatey feed or installed-tool lookup.

PR security tests verify signed fixtures using the production updater. They do
not perform live GitHub OIDC/Fulcio/Rekor release signing. The release signer
and its fixed `refs/heads/main` certificate identity belong to the post-merge
release workflow. Passing binary/fixture checks therefore did not establish
availability of the external signing service.

The edge caller also deliberately allowed publication after a failed platform
build, but did not separately require successful attestation. This produced the
misleading second failure when signing, rather than a platform build, failed.

## Repair and coverage

- Share NSIS preparation between release builds and the Windows PR matrix.
  Check PATH and the standard installed location before contacting Chocolatey;
  retry failed installation at most three times with five-second delays. Stop
  before compilation unless the compiler exists, even if the installer reports
  an error after placing it. Missing Chocolatey produces an explicit error;
  local diagnosis does not require an Actions environment.
- Run real Windows installer compilation in the PR frozen-verifier job, using
  the real freshly built executable.
- Compile the installer through one shared helper
  (`scripts/build_windows_installer.py`) in the release build, the Windows PR
  matrix and the Linux NSIS check. The file name comes from
  `scripts/sidecar_pkg/asset_names.py` and the output path is computed
  relative to the `.nsi` file, so the pull request gate and the release can no
  longer disagree about where the installer is written. The helper verifies
  the result and fails closed when the installer did not land at the resolved
  path, so a wrong output path or a lost output-path override stops the build
  instead of shipping an installer nobody looks for.
- Retry only the observed Rekor 409 equivalent-entry conflict, at most three
  times, launching a new cosign process and ephemeral key each time. Discard
  partial bundles before retrying or failing. Other signing errors fail closed.
- Keep release signature/identity, production updater and size verification
  mandatory. No unsigned release fallback or transparency-log bypass is added.
- Export readiness only after upload of the fully verified signed asset set.
  Gate edge publication on that readiness, preserving intentional partial
  platform releases while skipping publication after signing failure.
- Fault-injection tests cover conflict recovery/exhaustion, unrelated signing
  rejection, signing timeout cleanup, missing bundles, feed recovery/exhaustion,
  installed NSIS outside PATH, and an installer that lands at the wrong output
  path or without its output-path override. These establish control-flow behavior; they
  cannot establish external-service availability at the next release.

Live release signing remains a post-merge check. Windows provisioning now runs
on PRs, but a feed outage occurring only after merge can still fail a release
after the bounded recovery is exhausted. Such a failure must remain visible.
