# #773: no-update isolation control

The failed [hosted public-update attempt](claude-keychain-public-update-probe-773.md#hosted-ci-attempt-on-2026-10-23)
retained `before_update` and a timeout, then observed changed lock-state comparisons.
It did not identify the first boundary where state changed. This control tests
fixture setup, reads and a second process without calling an item-update API.

## Execution boundary

Use only the manually dispatched Synthetic Keychain investigation workflow on a
fresh GitHub-hosted macOS runner. Its default case is now `read-only-control`.
Other cases, including the original public update matrix (`all`), require explicit
selection. Personal machines and self-hosted runners remain quarantined. No real
credential access, OAuth exchange, permission prompt, database-password retention,
private Apple API, or production write-guard change is part of this experiment.

The fixture creates and seeds only a private synthetic Keychain. The separately
signed C helper performs its fixed service/account metadata lookup, verifies the
owning private Keychain and exits before any update. It returns only a completion
boolean. The original process verifies unchanged fake data and access policy;
cleanup removes its fixture.

The separately selected `reference-control` also resolves the exact reference
query used by the update cases, verifies the returned item and its owning private
Keychain, and exits without an update. Both paths use the same query builder.
This separates reference resolution from mutation: the pinned public
[legacy implementation](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/SecItem.cpp#L5017-L5059)
resolves the query before updating matches. That source-derived hypothesis is
distinct from proof about the current OS implementation.

## Recorded boundaries and failure behavior

Fresh-process metadata comparisons follow fixture creation, fixture seeding,
original-reader verification, policy capture, the helper, and final readback.
The existing post-cleanup comparison is retained. Reports contain fixed stage
names and equality booleans; paths, references, passwords and raw ACLs stay out of
artifacts. Any mismatch or unavailable comparison stops further fixture operations
and leaves cleanup responsible for deleting only that fixture.

Each completed checkpoint is atomically saved as `active_case_progress` before
continuing. A forced outer deadline or interruption therefore retains the last
observed boundary. Completed case rows include `isolation_checkpoints`,
`control_only=true` and `update_attempted=false`. Existing process-group/job
limits, denial suppression and nonzero incomplete/unsafe outcomes remain intact.

`selected_cases` states which cases actually ran. `matrix_complete` applies to
that selection: a passing no-update control does not mean the public update
matrix passed. A no-update control always reports `candidate_found=false` and
cannot qualify a supported renewal strategy.

## Acceptance remains open

The first changed boundary narrows the investigation; a single observation does
not establish the cause of the historical prompt loop or prove an update API
unsafe in every environment. A clean control rules out only the operations it
actually exercised. #773/#750/#526 remain open until the original current-package,
exact-item, policy-preserving renewal and coexistence criteria pass. Supported
provider authorization and release identity remain tracked in #781/#767.

## First hosted control result (2026-10-23)

The explicit [no-update control run](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40013777280)
on source `6007eef8` passed all seven metadata checkpoints. Synthetic data and
policy were unchanged, and the fixture was deleted. The [artifact](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40013777280/artifacts/12527830784)
is preserved in [sanitized JSON](claude-keychain-control-ci-773.json).

This control observed no isolation change during setup, ordinary metadata lookup,
readback or cleanup on that runner. It did not exercise the exact update reference
query or an update API, and `candidate_found` is false. The prior failed update
attempt remains unexplained; the historical personal-machine prompt loop remains
unattributed. These findings authorize no production write path.
