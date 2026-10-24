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
