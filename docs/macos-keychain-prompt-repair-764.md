# macOS Keychain prompt repair (#764)

Date: 2026-10-21. Delivery: draft PR #751. Related: #526 and native acceptance #750.

## Investigation

A fully synthetic successful renewal issued 21 native adapter operations (17
copies, one add, two updates, one delete). The old adapter launched a helper per
operation and allowed native UI. These counts are not observed dialog counts.
Metadata reads do not necessarily prompt, and a new PID does not by itself prove
that macOS invalidates an earlier consent choice.

Discovery independently invoked the interactive `security` executable. Two
concurrent synthetic discovery requests could both start before either denial
latched. Renewal permission did not disable read-only discovery. Safe Storage
and legacy generic registry readers also had UI-capable command paths.

After native acceptance helpers had exited, the installed production sidecar
remained active and logged another Claude discovery timeout at local 15:10:32.
It was stopped, and later process snapshots found no Runway requester. Its login
item has no crash restart but remains configured to launch at a future login.
Both installed and acceptance app bundles were ad-hoc signed. Exact historical
prompt owners, counts, item identities and button choices remain unverified.

## Repair

- Use one lazy helper per sidecar process and bounded framed private pipes. Both
  directions and lock waiting share a 30-second deadline; frame size is capped
  at 4 MiB. No token arguments, environment variables, diagnostic output, or
  plaintext recovery storage. The native worker lazily loads frameworks.
- Explicitly disable interaction around every legacy file-based native call,
  restoring its prior state afterward. Fail closed if policy setup/restoration
  fails; normalize both interaction-required/not-allowed statuses as denied.
- Route Claude Code discovery, Safe Storage reads and generic registry Keychain
  rules through the noninteractive adapter. Require unique exact-item selection.
  Service-only readers reject ambiguity. Desktop remains read-only.
- Serialize request and denial updates. Claude default/suffixed discovery,
  renewal and legacy Code rules share one suppression group. Other services keep
  independent groups. Recheck local write permission after queueing and helper
  startup, immediately before dispatch. Keep the default-off literal-true local
  permission plus effective keep-alive requirements.
- Do not replay uncertain requests. The existing renewal state machine retains
  all concurrency re-reads, intent/replacement verification, and recovery rules.
- Close and reap helpers on failure, shutdown and updater exits. Independent
  stdin monitoring terminates a worker after parent loss even during stalled
  native IO; a worker-side watchdog bounds native calls. Pipe setup is
  transactional, so failed initialization cannot strand a child.
- Config reload and failed update execution can reopen transport resources while
  retaining denial suppression. Generation-based cancellation prevents old
  queued calls from dispatching after a stop/restart. Cancellation does not
  poison permission suppression or Safe Storage caches.

The legacy interaction API is deprecated but addresses the existing file-based
Keychain. This repair does not move Claude's item, rewrite its ACL, unlock the
Keychain, or automatically enroll Runway as trusted. If existing permissions do
not permit unattended access, the outcome is unavailable, with no interactive
fallback. Native verification must establish this behavior on the actual OS and
package; a persistent helper alone is not proof of prompt-free operation.

## Verification

- `make lint && make test && make web-test`: passed; **5,284 Python tests passed,
  62 skipped; 1,208 frontend tests passed**. A subsequent Protocol-only CodeQL
  correction passed the 55-test file-renewer suite. Commit/push gates also run.
- Explicit-package-base mypy checks passed for the four native transport/adapter
  modules. Secret scanning of tracked files passed before staging.
- Native policy and transport tests cover partial frames, stalled stdin/stdout,
  malformed/oversized responses, timeout/denial isolation, concurrent callers,
  queued permission revocation, parent loss, watchdog expiry, cancellation,
  config reload, failed updater exec, and failed nonblocking pipe setup.
- Actual `KeychainStore` and `KeychainRenewer` run over the framed subprocess
  protocol against a synthetic persistent store. Seven fault scenarios cover
  failed intent, failed response/target writes, lost response persistence,
  interrupted verified target writes and cleanup. Pending/ambiguous tokens
  never cause another exchange. All credentials in those fixtures are synthetic.
- Independent review found the lifecycle-reopen and pipe-setup defects; failing
  regression tests reproduced both and passed after correction. The whole diff
  received a self-review. All four packaging specs include transitive modules.
- Rebuilt macOS app at `dist/764/Runway Sidecar.app` passed ad-hoc signature
  verification. Its headless worker rejected an invalid operation and exited
  cleanly without native dispatch, credential reads/writes, or OAuth exchanges.
  This is packaging/bootstrap verification, not native Keychain acceptance.

### Test-isolation incident

An initial new RED reader test mocked the new boundary while production still
used the old discovery path. It unintentionally reached the old native read-only
lookup and may have requested consent. The test was interrupted immediately;
no refresh or write was attempted and no requester remained. Captured test output
was removed without inspection. Do not describe the entire implementation run as
having made no credential-access attempt.

A suite-wide guard now blocks the legacy native command and native adapter
dispatch unless a test explicitly injects synthetic IO. Recovery integration
tests restore the adapter only after installing their synthetic worker command.
All later test runs used these guards. No further real credential test ran.

## Remaining acceptance

Keep #751 draft and #526/#750/#764 open. Runway remains stopped; no production
installation or background renewal activation was performed.

1. **Passed with explicit user approval:** isolated temporary Keychain containing
   four synthetic items. Source and packaged helpers completed read/update/read-back,
   helper reuse/restart, locked read/write denial, empty-trust ACL denial, repeated
   denial suppression, unaffected-group operation and deletion without recreation.
   No new SecurityAgent process was observed with a 50 ms monitor; this is a UI
   activity proxy rather than an exact dialog counter. All four fixture attempts
   restored the original search list/default and deleted their temporary Keychain.
   The first three attempts diagnosed a fixture trust mismatch: the SDK's current-
   application reference (`SecTrustedApplicationCreateFromPath(NULL, ...)`) fixed
   the fixture. No product or real-item ACL was changed, and no Claude credential
   read, refresh, or write occurred. Sanitized evidence is in
   [macos-keychain-prompt-repair-native-764.json](macos-keychain-prompt-repair-native-764.json).
   This used no additional Anthropic account.
2. Separately approve a bounded real-login read with zero exchanges/writes.
   Stop and tear down on the first unexpected dialog; do not keep approving.
3. Only after those pass, separately approve at most one renewal and verify
   expiry advancement, available access-control evidence, Claude Code acceptance,
   recovery cleanup, not-due cycles and the full packaged daemon.

If unattended access is unavailable, retain that result and create a linked
follow-up for explicit local authorization/stable-signing design. Do not hide
interactive enrollment inside the background loop. The independent Claude Code
write race and provider-rotation-to-journal crash interval remain documented.


## Existing draft review disposition

Two concrete Hermes findings were verified and corrected: temporary subprocesses
now receive an explicit project import path (the CI failure), and recovery only
recognizes a previous completed write when the full OAuth data matches. A valid
response reusing its refresh token now saves the new access token; interrupted
cleanup does not exchange again. The watchdog test also records entry into its
synthetic native call so an import failure cannot masquerade as watchdog success.

The requested timer-based intent retry is intentionally rejected: elapsed time
cannot prove that a single-use token was not consumed. Claude Code may renew or
reauthenticate, after which the changed login supersedes the old intent. File and
Keychain sweeps are separately serialized under the same per-user lock; a whole
provider sweep is not an atomic transaction, and each exchange re-reads under its
lock. Nesting the existing non-reentrant lock would skip renewal. Ambiguous reads
remain unavailable rather than selecting another profile's first item. The shared
30-second deadline includes queue time; no UI fallback is added. CFData references
remain opaque bytes across the private plist transport. A failed interaction-policy
restore deliberately retires the worker as unavailable instead of trusting its state.
No additional review request was made after the user's instruction.
