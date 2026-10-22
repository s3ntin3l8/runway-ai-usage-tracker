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
tests pass without loading native frameworks (14 tests). The C program compiles with warnings treated as
errors, and an invalid-scope startup control exits before any Keychain call.

The corrected full native matrix has not been executed. These changes improve
the investigation boundary; they do not solve cross-application policy
preservation. The production guard from #751 remains unchanged.
