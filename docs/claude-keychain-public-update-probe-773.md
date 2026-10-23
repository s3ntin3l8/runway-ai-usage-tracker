# #773 public update probe: work in progress

This investigation follows merged PR #751. It does not establish a supported
write-back path or complete #526/#750 acceptance.

## Experiment

The standalone native program in tests/native/keychain_public_update_probe.c
uses only public Security/CoreFoundation APIs. It accepts only an owned private
fixture file, verifies the selected item's owning Keychain, disables interaction,
uses fixed synthetic service/account/data, and prints only statuses and booleans.
It never invokes the security CLI, unlocks or locks a Keychain, changes user
Keychain preferences, or makes an OAuth request.

The Python runner creates a fresh protected fixture for each candidate and
compiles/signs a second executable identity. The intended comparison is:

- Attribute-only metadata control.
- SecItemUpdate data replacement.
- SecKeychainItemModifyAttributesAndData replacement.
- SecItemUpdate with data and the saved kSecAttrAccess object.

Success requires the replacement to be verified by the writer, canonical access
policy to remain equal, and the original process to read the replacement. An API
return code alone is insufficient. The probe is never imported by the sidecar or
run by pytest. It requires an explicit isolated-synthetic-only command switch.

## Observations on 2026-10-22

The initial metadata control timed out before producing a candidate result.
Cleanup's metadata assertion also failed. A later instrumented metadata control
again timed out, and its cleanup raised an error while deleting the synthetic
Keychain. These are failed/incomplete experiments, not evidence that any candidate
preserves policy or that all candidates are unsupported. The exact timed-out
native stage was lost when cleanup replaced the original exception.

The session also accumulated orphaned security find-generic-password requests
for GitHub authentication. Their commands did not target the synthetic service.
They continued after the native test processes exited. The user reported a
security prompt. The orphaned requesters were terminated, no synthetic writer
remained, and subsequent process checks found no security requesters. The merged
PR's live chat attachment was removed to eliminate its status polling as one
possible source. The spawning source and relationship to the native timeout have
not been established; removal/termination is not proof of causality.

Metadata checks found the login Keychain locked, no synthetic entries in the
search list, and no remaining private fixture directories. The first failed
comparison did not retain the detailed before/after state needed to explain
when the lock changed. No code in the probe locks the login Keychain. Do not
infer from this that the experiment caused or did not cause the state change.

Native testing is stopped until this incident is explained and the environment
is suitable. No real Claude token was read, refreshed, or written by this probe.
No real Keychain password or owner-authorization change was requested.

## Harness corrections

Two nonnative regressions reproduced:

1. Fixture cleanup did not explicitly disable interaction before deletion.
2. A cleanup RuntimeError could mask the timeout and prevent a sanitized report.

Cleanup now disables interaction. The probe retains the original timeout, filters
fixed stage markers, stops after an incomplete/isolation-failed case, and reports
failure rather than interpreting an incomplete matrix as a negative candidate
result. Both regressions, cancellation through failed cleanup, and the existing isolation
tests pass without loading native frameworks. The C program compiles with warnings treated as
errors, and an invalid-scope startup control exits before any Keychain call.

The corrected full native matrix has not been executed. These changes improve
the investigation boundary; they do not solve cross-application policy
preservation. The production guard from #751 remains unchanged.

## Latest isolation failure and containment (2026-10-22)

A bounded metadata-only control reached SecItemUpdate and timed out. It did
not complete the matrix. Temporary test Keychains were removed, but before/after
metadata reported changes to the default/search-list lock state. The default
Keychain and search-list paths were unchanged. No real Claude credential
operations or OAuth exchanges occurred. These observations do not establish
what caused the lock-state change or the recurring GitHub credential prompts.
They also do not establish that every public update strategy is unsupported.

The probe is now quarantined: it refuses execution before subprocesses or native
metadata unless explicitly enabled in a GitHub-hosted macOS job. The manual
Synthetic Keychain investigation workflow uses a fresh hosted runner, synthetic
credentials only, no repository/provider secrets, a process-group deadline and
a job deadline. Do not run it on a personal Mac or a self-hosted runner.
Incomplete, blocked or isolation-failed reports exit nonzero. No candidate may
be promoted to production without a complete policy-preservation result.

Production retains the unsupported-partition-policy guard. Safe refusal is not
renewal acceptance. Remaining native write-path work is tracked in #773; signing
and update continuity in #767; provider authorization in #781; real-item
acceptance in #750 and #526.

## Diagnostic follow-up (#849)

Nonnative tests now cover writer exits 64/65/66, allowlisted stage retention,
compilation/signing/signature-verification failures, interruption, process-group
deadlines, and Python/C synthetic lookup consistency. Reports contain fixed
phases, numeric statuses, and booleans; exception text, subprocess output, and
user paths are excluded. Atomic progress snapshots survive an outer deadline;
the supervisor marks the result incomplete and nonzero after killing and reaping
the probe process group. An incomplete matrix never reports a candidate.

The `security_cli_active` preflight checks only the `security` executable. It
does not detect permission dialogs, block the `securityd` daemon, or establish
what caused the historical isolation failure. The native fixed-target guard and
production unsupported-policy guard are unchanged. A fresh hosted CI execution
and its sanitized artifact will be linked here separately from native renewal,
real-account, signing, and update-continuity acceptance.
