# Keychain scraper consent and durable collection

Approved product plan, 2026-10-22. Tracker: #780. This replaces the earlier
proposal for blanket source opt-ins and expiring server credential leases.

## Permission boundary

Only Keychain-backed credential access needs the new local opt-in. Files,
environment variables, local usage logs, Firefox and Safari stores retain their
existing collection behavior. Chromium Safe Storage and Claude Desktop safeStorage
are Keychain dependencies and appear as separate scraper permissions. The macOS
GitHub CLI fallback must use bounded native IO, not launch gh/security requesters.

Each scraper starts off. Enable all grants read consent to the currently listed
catalog only. New scrapers start off. Consent binds a capability revision, a local
generation and the paired destination. Unknown IDs, truthy non-booleans, remote
resource substitutions and mismatched revisions cannot grant access. A shared OS
grant does not enable another scraper. The static catalog is generated from the
canonical credential registry and overlay by make sidecar-registry, without IO.

Renewal/write-back is separate and default off. Claude requires read consent,
literal-true claude_keychain_renewal, effective keep-alive and a supported exact-item
write path. Claude Desktop OAuth remains read-only. Fleet may narrow permission
but never enable local access or writes. Changing the paired destination requires
renewed local source consent; it does not manufacture a new OS grant.

## Setup and local controls (#782)

The local settings Sources section offers per-scraper Use Keychain controls,
Enable all, Disable all and a separate Claude renewal control. Saving intent,
GET/status requests and background ticks never open permission UI. Only a local
Authorize POST can launch the packaged foreground helper. One attempt runs at a
time, has a deadline, selects an unambiguous exact existing item, and is followed
by a fresh noninteractive worker check. Disabling or changing consent invalidates
late results and cancels a waiting foreground helper. No password is entered into
the settings page, passed as an argument, sent, logged or retained.

Keychain reads and writes use the existing private-pipe transport. Resource and
consent checks cover discovery, identity fallbacks, cached Safe Storage keys,
worker dispatch and egress. Denial/timeout suppression is shared by discovery and
renewal. Only successful explicit setup may reset the affected suppression group.
Original file-based renewers and local usage history remain intact.

States are Off, Setup required, Connecting, Ready, Limited, Action required and
Withdrawal pending. Read access alone cannot prove renewal, API acceptance or
permission continuity across updates. The first local-control milestone withholds
Keychain credential forwarding until the authenticated lifecycle below is
available. Previously forwarded legacy credentials require server-side retirement;
a local checkbox does not retroactively revoke a provider credential.

### Antigravity CLI on macOS

The Antigravity (agy) CLI keeps its OAuth token in the macOS login Keychain
(service `gemini`, account `antigravity`) from 1.4.3 on and writes no token
file, so it is a Keychain-backed scraper like the Claude Code one: it starts
off, needs the local read consent, and is authorized by the same bounded
foreground helper. The service holds a second entry — the Antigravity ACP
server's own token — so the account is part of the source's identity and is
covered by the capability revision; the foreground authorization selects that
one exact entry and never the ACP one.

The stored value is wrapped in go-keyring's envelope, which the reader
unwraps; the fields it maps are the ones the token file maps, so every
platform pushes the same credential shape and one Google account is filed
under one cache key. The entry's access list trusts only the application that
wrote it, so the read goes through the Keychain worker and never through a
credential requester. The macOS credential stays local until the withdrawal
lifecycle below exists, like every other Keychain-backed credential.

## Fleet source lifecycle and UI (#783 / #780)

Server collectors continue using eligible credentials while a sidecar is offline.
There are no heartbeat-based leases. A scraper contributes credentials instead of
requiring manual entry; multiple sidecars/manual/file sources remain independent,
even if they contain the same token. Disabling one source withdraws only its
contribution. Collection stops only when no eligible credential remains.

Introduce per-device authenticated identity through pairing, using random HMAC
secrets and existing encrypted storage. Hostnames are labels, not identity. Keep
legacy shared-key clients compatible without allowing them to impersonate enrolled
devices. Source identity includes device, scraper, origin/account and generation.
Withdrawal deletes matching secret cache material and persists a tombstone; late
ingest cannot resurrect it. Re-enabling uses a newer generation. Historical
usage_events and other contributions survive. Forward-safe database additions and
Python/TypeScript/API documentation updates belong to this protocol milestone.

Fleet shows local consent, authorization, remote pause, collection health, renewal
support, last success and report time. Offline/stale reporting is distinct from
server collection health. Remote controls pause, resume only a server-imposed
pause within existing local consent, revoke a contribution, or request local
setup. Setup requests create a notice, never a native prompt. Commands use durable
IDs, ordered generations and idempotent acknowledgments over sidecar polling.

Offline opt-out shows local Off / withdrawal pending until acknowledged; direct
server revocation takes effect there immediately. Old peers are explicitly Limited.
New Keychain scraper — opt in now announcements appear in both UIs, scoped to
compatible sidecars and capability revisions. Dismissal never grants consent.

## Native safety and renewal acceptance (#773 / #767 / #781 / #750 / #526)

Do not run native probes on the everyday Mac. Synthetic native tests run only in
disposable hosted macOS CI, with no provider credentials or release secrets,
process-group/job deadlines and isolation/cleanup checks. Incomplete experiments
fail. The latest metadata-only public update control timed out and reported lock
metadata changes; causality of those changes and recurring credential prompts is
unresolved. See claude-keychain-public-update-probe-773.md. Subsequent #886
controls preserved observed isolation and demonstrated a creator/helper read
boundary, not a supported shared-access store. The current
[source renewal readiness inventory](source-renewal-readiness-781.md) separates
implemented mechanisms, historical evidence and remaining acceptance gates.

Retain the unsupported-policy write guard until a public supported write path
proves policy preservation. Prefer an independently authorized Runway grant or a
proved owner-cooperation/shared-store mechanism. A copied Claude Code refresh token
is still shared ownership. Borrowed refresh tokens and recovery records stay
local; retain server-side prevention of CLI token rotation.

Exact-item identity, unrelated JSON fields, account/service/access policy, per-user
locking and pre-exchange/pre-write re-reads must be preserved. Recovery persists
verified intent before exchange and replacement before target write; pending
replacements never cause another exchange. Verify target read-back before cleanup,
never recreate deleted credentials, preserve independently newer logins and block
ambiguous interrupted refresh reuse. Keychain has no atomic comparison against an
independent Claude Code refresh; the provider-response-to-journal crash gap remains.

Developer ID signing is not available yet. It is an external gate for the intended
release identity and signed A-to-B update/rollback acceptance, not for synthetic
development. Stable signing alone does not prove access survives owner-side
credential replacement. Real-account acceptance requires a readiness report and
explicit credential-access/rotation approval in an isolated environment. No new
Anthropic account is required. Keep #526 and native acceptance trackers open until
their actual criteria pass. Verified intermediate milestones may ship honestly.

## Cloud usage and delivery (#669)

Revalidate the previously investigated Claude session/history interface before
implementing server-side cloud collection. Exhaust pagination, bound backfill,
checkpoint after event commit and reconcile assistant records versus cumulative
counters, resets, resumes, subagents and delayed updates. Deduplicate local/cloud
overlap and distinguish execution environment, sender and viewing activity.
Parse responses in memory and persist only usage fields, without transcripts.
Keep local fallback until coverage/accounting parity is demonstrated; do not claim
all Chat/Desktop/Cowork/mobile history is covered. Server collection continues
while credentials remain usable; borrowed CLI refresh is never moved server-side.

The [offline accounting audit](claude-cloud-accounting-669.md) records SDK usage
observations and the live-verification gates; it does not enable cloud collection.

Delivery order: #773 safety containment; #782 local consent; #783/#780 Fleet
lifecycle and UI; #767/#781 release identity and supported renewal; #669 cloud
collection. Run make lint, make test, make web-lint and make web-test for each PR,
self-review the diff, preserve coverage/security gates and request no additional
review while the user's instruction remains. Track every blocked/deferred gate in
linked GitHub issues. Green tests or safe refusal do not prove native acceptance.
