# #773: no-update isolation control

The failed [hosted public-update attempt](claude-keychain-public-update-probe-773.md#hosted-ci-attempt-on-2026-10-23)
retained `before_update` and a timeout, then observed changed lock-state comparisons.
It did not identify the first boundary where state changed. This control tests
fixture setup, reads and a second process without calling an item-update API.

## Execution boundary

Use only the manually dispatched Synthetic Keychain investigation workflow on a
fresh GitHub-hosted macOS runner. Its default case is now `read-only-control`.
Other cases, including the original public update matrix (`update-matrix`), require explicit
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

The separately selected `writer-read-control` resolves and verifies that same
reference, then asks the second process to read the fixed fake value and compares
it in memory. It never updates an item and emits only completion/status fields.
This distinguishes metadata lookup from the second process's access to protected
data; it tests a hypothesis rather than assuming that a matching metadata
reference proves data access. No provider or real credential is involved.

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

The subsequent explicit [reference-control run](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40014383316)
on source `440c5cfe` also passed all checkpoints, unchanged synthetic data/policy
and cleanup. The returned reference matched the original private item and its
owning Keychain. Its [artifact](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40014383316/artifacts/12528185803)
is preserved in [reference evidence](claude-keychain-reference-ci-773.json).
It observed no isolation change while resolving the update query on that runner;
it did not decrypt data in the second process or call an update API.

## Protected-data control result (2026-10-23)

The explicit [writer-read-control run](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40015300225)
on source `c8e55fd6` failed closed. Exact-reference lookup and owner verification
completed, but the second process's protected-data read returned status `-25293`
and `control_completed=false`. It did not verify the fake value. No update was
attempted, and no original-reader readback followed the denial. All six observed
isolation checkpoints, including cleanup, remained unchanged; the private fixture
was deleted. The [artifact](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40015300225/artifacts/12528610825)
is preserved in [protected-read evidence](claude-keychain-writer-read-ci-773.json).
The workflow correctly failed with an incomplete selected matrix and no candidate.
The denied operation was not retried.

These separate hosted runs show that successful metadata and exact-reference
lookup do not establish that the second process can read protected data. They
identify an access boundary that must be understood before another update
experiment can yield interpretable policy-preservation evidence. They do not
establish why this read was denied, why the earlier update timed out, or what
caused the historical personal-machine prompt loop. The observed lock-state
change was not reproduced by this denied read.

The next investigation must explain the synthetic creator/helper authorization
boundary using sanitized policy and stable identity evidence in disposable CI.
It must not fix the fixture by granting broad access or relaxing the production
write guard. No supported shared-item renewal route has been proved. #773 and
#781 retain that work; #767 retains signed-release identity and update continuity.
Real-account/package acceptance under #750/#526 remains deferred until that route
and an isolated acceptance environment are ready.

## Review hardening

All selected cases now retain durable isolation checkpoints, including update
modes. A changed or unavailable comparison stops subsequent fixture operations;
this safety rule applies to controls and updates alike. The matrix selector is
named `update-matrix` to distinguish the four original update cases from the
three separately selected controls.

New reports distinguish `control_denied`, `control_read_failed` and
`control_data_mismatch`. Update reports retain `read_status` and report
`writer_read_denied` or `writer_read_failed` without subsequent credential
readback. `replacement_verified=null` in those cases means verification was
unavailable, not that the original data was preserved. Such a result can never
qualify as a renewal candidate. Historical committed reports remain unmodified;
their original `native_probe_incomplete` diagnostic predates this classification.
These changes were validated with nonnative tests; the recorded hosted controls
were not rerun after denial.
