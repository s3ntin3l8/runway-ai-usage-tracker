# Source consent and durable collection

The durable reference for the **Sources consent model** — which local
scrapers a sidecar may read, who grants that permission, and what happens when
a source is withdrawn.

Approved product plan, 2026-10-22. Tracker: #780. This replaces the earlier
proposal for blanket source opt-ins and expiring server credential leases.

> The delivery plan and the numbered findings that accompanied this approval
> are archived in
> [`audit/2026-10-sidecar-source-authorization-delivery.md`](audit/2026-10-sidecar-source-authorization-delivery.md).

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

The local settings Keychain scrapers section lists apps and CLIs one row each and the
browser-cookie sources one row per service. A **Read from** selector chooses which
browsers' cookie stores may be read (all three until one is unticked; unticking turns that
browser's sources off, and ticking it again turns nothing on). It also offers Enable all, Disable
all and a separate Claude renewal control. Saving intent,
GET/status requests and background ticks never open permission UI. Only a local
`authorize` or `authorize_all` POST can launch the packaged foreground helper.
**Authorize all enabled** walks the sources that are on and not authorized yet, one Keychain
item at a time: sources that read the same item (every service in one browser; Claude Desktop's
OAuth token and session) share a single helper launch, and each source behind it is then
verified on its own. It can be cancelled, which stops after the item being authorized. One attempt runs at a
time (single-source and batch share the same flight), has a deadline, selects an unambiguous
exact existing item, and is followed by a fresh noninteractive worker check. Disabling or changing consent invalidates
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

## Related

- [`sidecar.md`](sidecar.md) — the sidecar itself: install, CLI, configuration.
- [`collectors/antigravity.md`](collectors/antigravity.md) — Antigravity as a
  Keychain-backed scraper.
- [`evidence/README.md`](evidence/README.md) — point-in-time acceptance records.
- [`audit/README.md`](audit/README.md) — the audit reports and the frozen snapshots.
