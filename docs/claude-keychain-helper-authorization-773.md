# #773: synthetic creator/helper authorization boundary

The [previous protected-read control](claude-keychain-isolation-control-773.md#protected-data-control-result-2026-10-23)
returned `-25293` after exact-item and owner verification. It made no update and
observed no default/search/lock-state change. This investigation does not repeat
that denied read or infer its cause from the status alone.

## Two independently selected hosted controls

`authorization-metadata` uses the existing Python-created fake item. The helper
reads only its ACL metadata, never the protected value. The original Python
reader verifies its own fake value and unchanged policy before/after the helper.

`helper-owned-control` creates an empty private fixture in Python, then launches
the signed helper in `helper-seed` mode. The helper adds only the fixed fake
service/account/value with an explicit two-application trust list: itself and its
Python parent, whose executable path comes from `proc_pidpath(getppid())`.
No password is supplied to the helper. The seed process exits; another invocation
of the same signed binary verifies the exact item/owning Keychain, captures
metadata, and reads the fake value. The harness compares binary hashes in memory
before the second invocation and emits only an equality boolean. Python then
checks its own read access and policy preservation. Failure of that separate
Python read does not erase the helper's evidence or prove a valid shared store.

Each case must run once in a separate fresh GitHub-hosted macOS job. Neither
changes existing ACLs, partitions, search/default Keychains or lock settings.
The legacy update modes are not dispatched as part of this milestone. Any denial,
invalid report, timeout or isolation failure stops subsequent credential work;
only scoped cleanup and metadata isolation checks follow. Existing deadlines,
process-group reaping and durable checkpoints remain in force.

## Interpretation of metadata

The helper inspects its own code and its Python parent's code through public
Security APIs. It selects the same identity representation as the production
partition guard: team identifier if present, otherwise code-directory hash.
Identifiers remain in memory. `identity_partitions_equal` compares those selected
representations, not executable equality or the OS authorization decision.
`fixture_creator` identifies which process created the item; Python is always the
separate reader, including when the helper is the item creator.

Ordinary decrypt ACLs are compared against freshly obtained trusted-application
metadata using public `SecTrustedApplicationCopyData`. Exact blob equality is
reported as such: inequality does not prove exclusion because equivalent code
requirements can have different representations. Broad decrypt access is
reported separately, never introduced as a workaround.

Partition descriptions are decoded in memory as the existing hex-encoded plist
shape. Membership booleans compare the selected identities with the actual
partition entries. They do not interpret Apple-specific partition semantics or
prove access. Missing, malformed or unavailable metadata remains `null` with
availability booleans; it is never converted into an inferred match. A nonzero
ACL metadata status fails the case and stops item operations.

Reports allow only fixed field names, booleans/null, numeric status and fixed
categories. They contain no paths, code hashes, team identifiers, application
blobs, raw ACL descriptions, passwords or credential values. Historical reports
remain immutable. No real Claude credential or provider exchange is involved.

## Acceptance boundary

These controls can establish a creator/helper access difference and whether the
fixture supports a fresh helper plus a separate Python reader. They cannot alone
establish its cause, a supported cross-application write architecture, or durable
release consent. Do not call another update matrix until the evidence supports a
separately reviewed experiment. #773/#750/#526 stay open; supported authorization
remains #781 and signed-release/update continuity remains #767. Personal-machine
native probes remain quarantined.

## Hosted results: 2026-10-24

Both runs used immutable source [25025ab9](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/25025ab95870620c5eb50eb3001788095ecdcb5f),
one case per fresh hosted job. They made zero real credential operations and zero
OAuth exchanges. No item update was attempted, and neither denied read was retried.

| Observation | Python-created item | Helper-created item |
| --- | --- | --- |
| Python trust blob equals fresh Python metadata | true | true |
| Helper trust blob equals fresh helper metadata | false | true |
| Broad decrypt access | false | false |
| Partition contains Python selected identity | true | false |
| Partition contains helper selected identity | false | true |
| Fresh helper protected-data read | Not attempted; historical denial retained | Verified, status 0 |
| Separate Python read | Verified | Denied |
| Access policy/integrity | Unchanged | Unchanged |
| Default/search/lock comparisons and fixture cleanup | Passed | Passed |

The [metadata-only run](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40075912653)
passed. Its [sanitized report](claude-keychain-creator-metadata-ci-773.json) retains
all seven checkpoints and the original Python data/policy verification.

The [helper-owned run](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40076029539)
failed closed because the separate Python reader was denied, despite both ordinary
trust-blob comparisons being true. Its [sanitized report](claude-keychain-helper-owned-ci-773.json)
retains successful seeding, unchanged helper binary, exact reference/owner
verification, successful fresh-helper data read and unchanged policy/integrity.
All seven observed isolation checkpoints passed and the fixture was deleted.
The historical report's failure is `native_probe_incomplete`; subsequent orchestration
now explicitly labels this condition `original_reader_denied`. This classification
was validated with mocks without repeating the native denial.

### What this establishes

The separate helper can read a protected item created by an earlier invocation of
that same binary. The harness therefore supports that fresh-process same-helper
control, but it has **not established a valid fixture with shared protected-data
access for Python and the helper**. Two ordinary trusted applications do not by
themselves demonstrate such access on this fixture. Across these runs, the selected
partition identity tracks the item creator, and protected reads succeed for that
creator while the other process is denied. This supports a partition-related
explanation; it is not a controlled proof of the OS authorization algorithm.
The Python-created item's unequal helper trust blob is an additional unresolved
representation difference and must not be silently attributed solely to partitions.

These observations do not explain the historical update timeout, changed lock
comparisons or personal-machine prompt loop. They prove no supported cross-app
write path. There was no further native execution, update matrix or real-login
acceptance after the helper-owned failure. The next authorization work remains
#773/#781; stable signing remains #767. Require a separately reviewed, supported
shared-access/owner-cooperation design before another write experiment, without
broad trust, partition editing, private APIs or Keychain-password retention.

## Review hardening

`original_reader_denied` deliberately applies to **every selected case**, including
`metadata-control`, `data-only`, `legacy-data` and `combined-access` in the deferred
`update-matrix`. Unlike the older harness, an original-reader denial stops the
remaining matrix: the report retains the denied row, cannot become a renewal
candidate, marks the selection incomplete, and exits nonzero. Only scoped cleanup
and isolation metadata checks follow; later cases cannot perform another operation.
Nonnative tests exercise this rule for each update mode. This does not authorize
execution of that matrix in this investigation.

Malformed authorization blocks now report `invalid_authorization_metadata`.
The metadata-only native response reports both `control_status` and a completion
boolean derived from that status, matching the helper-owned response. Historical
artifacts retain their original payload and are not rewritten. These review edits
were checked with nonnative tests and syntax-only C compilation; no native denial
was repeated. Isolation-stop tests cover both new cases, including each boundary
around helper seeding and the fresh reader.
