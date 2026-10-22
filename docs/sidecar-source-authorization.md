# Sidecar source opt-ins and durable authorization

Date: 2026-10-22. Product tracker: #780. Design status: proposed for implementation review.

## Intent and approved product outcome

The user wants to enable individual scrapers in the sidecar UI, complete setup
once, and then have collection and supported keep-alive work unattended through
normal restarts and updates. Switching a source off stops its access and renewal.
The user approved working toward this description after distinguishing Runway's
connection from keeping Claude Code itself logged in.

Success is a durable Runway connection. Writing Claude Code's existing item is
not a required implementation choice for this new product direction. This does
not mark the older #526/#750 acceptance passed or erase its outstanding evidence.
No new Anthropic account is a prerequisite. Keeping another application's own
login alive is outside this product milestone unless separately requested.

The implementation must not retain the user's login-Keychain password, loop on
permission prompts, silently broaden Keychain trust, or copy a shared rotating
refresh token and describe the copy as an independent authorization.

## Current evidence and constraints

- sidecar_app/settings_server.py currently offers server settings, pairing,
  status and logs; there are no local source controls.
- scripts/sidecar.py evaluates file/env/exec/Keychain/browser rules, has additional
  identity/metadata reads, caches credentials, extracts local events and sends
  credential manifests. Consent must cover all those paths, not only a UI toggle.
- sidecar_app/daemon.py arms Claude keep-alive on Darwin in PR #751, subject to
  the local literal-true Keychain write gate and effective keep-alive. Fleet
  desire does not establish local source permission.
- .github/workflows/sidecar-build.yml explicitly signs macOS bundles ad-hoc.
  The macOS bundle identifier is com.runway.sidecar. No Apple-signing-named secret
  was visible in repository secret-name metadata; organization/environment
  secrets and local certificate stores were not inspected. A usable Developer ID
  identity remains an external prerequisite, not a fact inferred from this check.
- The exact PR #751 package passed 51 native cases with synthetic credentials.
  Its real-item metadata preflight rejected the current Claude policy before
  token reads or exchanges. Historical successful real refreshes predate that
  guard. See docs/claude-keychain-isolation-773.md.
- Classic data writes can regenerate the writer's partition policy. This matters
  both for Runway writes and for later owner-side replacement of a shared item.
  Stable signing alone is not proof that another application's credential policy
  will continue to admit Runway. Owner-side rotation is an acceptance test.
- The official Claude authentication documentation establishes Console API keys,
  workload federation and App Attest for API use. It does not establish a Runway
  authorization grant for subscription quota collection. Console billing and
  Messages-only authorization are different data/capabilities. The required
  independent subscription-usage path remains unproven in #781.
- The browser helper under scripts/security_browser_helper.py is a security test
  fixture, not an existing product browser extension or credential integration.

## Product behavior

The local Sources section lists individual sources, not a blanket Keychain
permission. Claude Code, Claude Desktop, browser sessions and local usage files
remain distinct. Provider/source names explain what data is read and whether
access credentials are sent to the paired Runway server. The server destination
is visible before authorization. Consent is bound to that destination: re-pairing
to another server requires explicit destination confirmation for enabled sources
before credential forwarding resumes, without repeating an unchanged OS grant.
No token or master-Keychain password is exposed
in the local web page, logs or status responses.

A source has an enable control, collection state and renewal state. The enable
action discloses its collection and eligible automatic-renewal scope; Fleet cannot
add a capability the user did not enable. Enabling
records local intent and reveals the appropriate setup action; saving a checkbox
or rendering a page never opens a macOS permission dialog. An explicit local
Authorize/Connect action starts one bounded foreground setup attempt. Some
sources require more than one permission category during that setup, so the
promise is no repeated setup in normal operation, not exactly one OS dialog.
Sources that need only local consent do not manufacture an OS prompt; they proceed
through a bounded noninteractive functional check. Creating a Runway-owned record
is distinct from requesting access to another app's existing Keychain item.

The app presents concise user states:

| State | Meaning and available action |
| --- | --- |
| Off | No local source operations are admitted. Enable is available. |
| Setup required | Local opt-in exists; an explicit authorization or connection step is needed. |
| Connecting | One foreground attempt is in progress; duplicate actions are disabled. |
| Ready | Collection and every requested, supported capability have been verified. |
| Limited | Collection works but requested renewal or update continuity is not established; show the missing capability. |
| Action required | A previously working grant was denied, revoked, expired or became ambiguous; offer explicit reconnect. |
| Off, withdrawal pending | Local access is off; the disconnected/older server has not confirmed withdrawal of previously forwarded access credentials. |

Collection and renewal are reported separately within the source row. A read
approval never turns renewal green by implication. A normal background tick may
use an established connection, but cannot display authorization UI. A denial or
timeout stops further automatic credential operations for the affected source;
retry requires a deliberate local setup/reconnect action. An OS/account reset or
revocation may legitimately require setup again; normal signed updates must not.

Unsupported provider renewal is shown honestly during development, but does not
satisfy the intended Claude unattended-renewal acceptance. A milestone may ship
collection controls without declaring the entire end state complete.

## Local consent and source isolation

Use a static catalog of source IDs and declared capabilities. Building the catalog
must not scan files, Keychain, browsers, environment credentials or CLI sessions.
Derive registry-backed descriptors from the canonical registry/generator; never
hand-edit scripts/sidecar.py's generated credential block. Supplemental sources
such as local logs and Desktop storage need explicit descriptors and owners.

Permission binds to the trusted catalog's resources and operations plus locally
approved directories/accounts, not a source ID supplied by the server. Remote
registry rules cannot reuse an enabled ID to add a foreign path, executable,
Keychain service or cookie domain. Remote directory suggestions stay inactive
until within that locally approved scope. Test resource substitution even when
the claimed provider/source ID is enabled.

Consent is a versioned local, owner-only configuration record containing source
IDs, exact booleans, requested capabilities and a persisted policy revision and a fresh runtime
policy generation. It contains no provider tokens or Keychain passwords. Unknown
IDs, malformed values and truthy non-booleans never authorize access. No public
API or server database change is needed for the first local-control milestone.

Pairing, providers=[all], existing automatic discovery, Fleet settings, remote
login-directory suggestions and old global keep-alive flags do not grant local
source consent. Existing installations enter a source-review migration rather
than silently authorizing everything. Headless users have explicit local source
configuration; there is no unattended OS-authorization fallback.

Every operation carries source provenance and the policy generation: discovery,
account hints, credential cache lookup, event extraction, identity verification,
renewal, native worker dispatch, offline queue replay and payload forwarding. Check local permission
before IO and recheck before using/forwarding a result. Disabled-source sentinels
must prove that low-level fallback/identity paths cannot read around the policy.

An OS grant may cover a shared resource such as browser Safe Storage. That grant
never enables another source or provider that remains locally off. Likewise,
remote keep-alive desire may schedule renewal only inside locally permitted and
proved source capabilities. Disable/reload cancels queued old-generation work,
invalidates matching cached credentials and drops late results. It must preserve
other sources' caches and historical usage.

The credential-free offline queue is also an egress boundary. Retain internal
source provenance on queued batches, check current consent again at replay and
drop unsent records for disabled sources. Legacy queued records with unknown
source provenance are withheld during migration. Do not delete original usage
files or already-ingested history, and do not add credentials to the queue.
Credential candidates retain their original source; never assemble access and
refresh material from different sources and infer one authorized grant.

A request already accepted by a provider cannot be unsent. Recheck permission
before exchange and each write/forwarding boundary. Preserve the existing
no-new-exchange/no-write-after-disable boundary; an interrupted rotating exchange
can leave a recovery-required state rather than silently restoring permission.
Recovery data stays local and is not processed while local permission is off.

## Foreground authorization and stable application identity

Use the actual packaged app/helper for setup and background IO. Preserve a stable
bundle/helper identity and designated requirement across legitimate updates.
Developer ID signing and notarization/Gatekeeper verification are separate from
Sigstore updater authenticity; both release contracts must be verified. Never
weaken the existing Sigstore/freshness/installer gates to introduce signing.

Keep source and account selection bound to the local setup attempt. Missing or
ambiguous matches stop before secret access and require selection/reconnection.
Setup results must be verified by a fresh noninteractive packaged worker before
Ready. Persist user intent, not a forged permission-granted boolean. A stored UI
state cannot substitute for actual OS and provider access.

Native prompts are created by the signed native process, never by a web page
request for the login-Keychain password. Settings POSTs retain existing exact
Host, same-origin/CSRF checks, safe body limits and strict source/action validation.
GET/status/polling endpoints are inert. Fleet and ordinary network requests cannot
create a foreground grant. Explicit reconnect starts at most one new bounded
local attempt and must not reset all other sources' denial suppression.

Acceptance must use two immutable signed builds A and B with the intended release
identity: authorize A, verify collection/renewal, install B through the real
updater, then verify fresh-worker behavior and rollback. Also test replacement of
an externally owned item by its owning application. Ad-hoc or source-only builds
are insufficient to claim durable permission across release updates.

## Credential ownership and renewal

Each adapter declares separately: collection method, credential ownership,
provider scopes, renewal mechanism, secure storage and expiry semantics. The
available strategies are:

1. A provider-supported independent authorization for Runway. Store its own
   refresh/recovery credentials locally in a Runway-owned Keychain item. Another
   application's refresh token copied into this item is still shared ownership
   and does not qualify.
2. A proved shared-store or documented owner-cooperation mechanism. Existing
   file renewers are not removed simply because their grant is shared: retain
   them where exact write-back, identity, permissions, concurrency and owning-CLI
   acceptance are proved. User-authorized shared-store renewal updates the same
   login safely; storing a second copy of its token does not create independence.
   Claude Keychain writes retain their current policy guard until equivalent
   permission preservation and coexistence are proved.
3. A read-only external source while neither renewal strategy is established.
   Report the limitation; do not label this the final unattended solution.

Do not assume one provider's OAuth implementation or a registered first-party
client can be reused as Runway's identity. Establish the actual required quota
endpoint/scopes and distinct grant, not just a successful token exchange. For
Claude, Console API billing and model requests do not replace subscription quota
collection. No model call is used merely to force refresh in acceptance.

Retain the existing per-user serialization, exact target matching, durable intent
and replacement journaling, no duplicate exchange on pending recovery, no
recreation of deleted credentials, sanitized outcomes and failure suppression.
Only access credentials and expiry needed by the paired server are forwarded.
Independent grants still require concurrency/recovery tests; a new storage name
alone solves none of those races.

## Turning a source off and the paired server

Local opt-out is effective immediately for new local work. Previously forwarded
access credentials also need source-scoped withdrawal; merely omitting the next
ingest does not demonstrate that the server stopped using its cached token.

The server protocol must bind source identity and consent generation to an
authenticated sidecar. Do not assume a shared ingest key proves one particular
machine. Reject cross-machine/source/account withdrawal. Retire matching cached
credentials and collector state using existing credential-source/token-cache
services. Never delete authoritative usage_events or another source's credentials.
Late old-generation ingest must not reactivate a withdrawn source.

Define renewable, expiring leases for server use of forwarded credentials. If the
sidecar is offline, show local Off with withdrawal pending until acknowledged;
server use ends no later than the agreed lease expiry. Old peer versions require
explicit capability negotiation and partial-support behavior. Lease duration,
identity binding and additive protocol/schema are design details for #783; they
must be settled and tested before claiming complete remote opt-out. No silent
schema/API change is hidden in the local UI milestone.

Denied, timed-out, missing and ambiguous scans are not withdrawal. Preserve last-
good records unless there is authoritative replacement/logout or explicit opt-out.

## Delivery order and responsibilities

| Milestone | Tracker | Files/boundaries and required result |
| --- | --- | --- |
| Local source controls | #782 | New scripts/sidecar_pkg/source_consent.py and source_catalog.py; sidecar_app/settings_server.py/config.py/daemon.py; scripts/sidecar.py and keep_alive.py; adapters/fallbacks/cache/egress. Off means zero source IO and forwarding; UI state is honest. |
| Release identity and foreground setup | #767 | macOS spec/build/updater verification, signed native authorization/worker boundary and per-source setup actions. Two-release continuity is demonstrated, not inferred. |
| Independent provider authorization | #781 | Per-provider ownership/scopes/adapters; secure local owned storage and renewal/recovery. Claude subscription-usage grant or supported owner cooperation is proved before enabling renewal. |
| Server withdrawal and leases | #783 | Fleet schema/endpoint capability negotiation; authenticated source lifecycle; credential_sources/token_cache/collector retirement and stale-generation rejection. Local off has bounded remote effect without deleting history. |
| End-to-end product acceptance | #780 | Settings opt-in through native/provider setup, collection, due renewal, signed update/rollback and opt-out. Existing #750 remains the older exact-item acceptance record. |

Use the repository's existing Python sidecar, local settings server and native
worker. Do not replace the tray/UI stack or add a browser extension as an
unproved shortcut. General source descriptors can cover all existing rule types,
but integrate providers incrementally with explicit capability/acceptance rows.
The first implementation is #782; it is not a substitute for completing the
remaining milestones. Old #526/#773 constraints remain documented and open.

## Verification and completion criteria

- Unit tests inject synthetic IO for each rule/source kind. Off, malformed
  consent, unknown IDs and remote instructions dispatch zero IO, cache reuse or
  credential forwarding. Include account-hint and local-event fallback paths.
- Exercise enable/disable/reload races, cancellation, stale generations, cache
  invalidation, destination changes and late responses from the old pairing,
  shared browser-resource isolation, mixed credential candidates,
  offline queue replay, provider errors and restart.
- Settings tests verify save/reload/migration, inert GETs and CSRF/Host defenses;
  secret-shaped values never appear in UI/status/log/error output.
- Native tests use explicitly scoped disposable Keychains, no default/search-list
  setters or locks, fresh user-state comparisons, helper reaping and cleanup.
- Per-provider tests prove API/quota semantics, scope/expiry handling, actual
  ownership, due renewal, durable recovery and no duplicate exchange.
- Server tests cover authentication binding, offline withdrawal, old peers,
  leases, late-ingest resurrection, multiple machines/accounts and retained
  event history.
- Native release acceptance verifies A-to-B update/rollback, fresh helper access,
  owner-side item rotation, provider acceptance and no routine prompt loop.
- Run make lint, make test and make web-test before pushing each code change.
  Preserve repository coverage and security gates. Review the diff yourself;
  do not request a new external review while the user's instruction remains.

A complete end-state claim requires actual UI/native/provider/update evidence for
the selected source capabilities. Green CI, a checkbox, read consent, a copied
refresh token, a synthetic-only exchange or safe refusal of a write is not enough.

## Scope decisions and unresolved prerequisites

User-approved: local opt-ins, one-time normal setup, update continuity, unattended
collection/keep-alive, immediate opt-out and work toward Runway's own connection.

Builder choices proposed here: keep the existing UI/runtime stack; local exact-
true generation-based policy; distinct capability states; a migration review;
separate signing/provider/withdrawal milestones; and source-scoped server leases.

Not established: a provider-supported Runway Claude subscription-usage grant,
release signing/notarization credentials, owner-write consent continuity or the
withdrawal lease/peer protocol. These are tracked deliverables, not assumptions.
No real-account access, password entry, permission change or rotation is required
to review this design or implement the synthetic local-policy milestone.

## References

- [Apple: code signing and Keychain tracking](https://developer.apple.com/library/archive/technotes/tn2206/_index.html).
- [Apple: classic Keychain data update](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/Item.cpp).
- [Claude API authentication](https://platform.claude.com/docs/en/manage-claude/authentication).
- [Claude Code authentication](https://code.claude.com/docs/en/authentication).
- Existing investigation: docs/claude-keychain-one-time-authorization-773.md,
  docs/claude-keychain-isolation-773.md and their sanitized evidence.
