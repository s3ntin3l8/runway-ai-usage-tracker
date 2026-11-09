# Source consent

Which local sources a sidecar may read, who grants that permission, and what
happens when it is withdrawn. Install and configuration are in
[`sidecar.md`](sidecar.md); the wire protocol in
[`fleet-protocol.md`](fleet-protocol.md) and the routes in
[`api-reference.md`](api-reference.md#fleet-keychain-source-lifecycle-protocol-1).

## What needs consent

Only Keychain-backed credential access needs an explicit local opt-in. Files,
environment variables, local usage logs and the Firefox and Safari cookie
stores are collected as before. Chromium Safe Storage and Claude Desktop
safeStorage are Keychain dependencies, so they appear as separate scrapers.

Every scraper starts **off**:

- **Enable all** grants read consent to the scrapers currently listed. A scraper added by a later release starts off.
- Consent is bound to the scraper's capability revision, a local generation and the paired server. A different server needs consent again (without creating a new OS grant).
- A shared OS grant never turns on another scraper.
- Unknown ids, non-boolean values and mismatched revisions grant nothing.

Renewal (writing a refreshed login back) is a separate permission and is off
by default. Claude Code Keychain renewal needs read consent, a literal
`claude_keychain_renewal: true` in the local config, effective keep-alive and
a supported write path. Claude Desktop OAuth is read-only.

## Granting consent locally

The sidecar's local settings page has a **Keychain scrapers** section: one row
per app or CLI, one row per browser-cookie service, a **Read from** selector
for which browsers' cookie stores may be read (unticking a browser turns its
sources off; ticking it again turns nothing on), **Enable all**, **Disable
all** and a separate Claude renewal control.

Saving a choice, status requests and background ticks never open an OS
prompt. Only an explicit local **Authorize** (or **Authorize all enabled**)
starts the packaged foreground helper, which walks the enabled, not-yet-authorized
sources one Keychain item at a time. Sources that read the same item share
one helper run; each is then verified on its own. One attempt runs at a time,
with a deadline, against one exact existing item, and it can be cancelled.
No password is typed into the settings page, passed as an argument, sent or
logged. Turning a source off or changing consent cancels any waiting helper.
A denied or timed-out Keychain prompt suppresses further access to that item
until a successful explicit setup.

## States

Each source shows one of: **Off**, **Setup required**, **Connecting**,
**Ready**, **Limited**, **Action required**, **Withdrawal pending**. Read
access alone does not prove renewal, API acceptance or that a permission
survives an app update. Old peers that cannot report source state appear as
**Limited**.

## What Fleet can and cannot do

Fleet shows each machine's local consent, authorization, remote pause,
collection health, renewal support, last success and report time. Offline or
stale reporting is distinct from server collection health: the server keeps
using eligible credentials while a sidecar is offline.

Remote control can only narrow or ask:

- **pause**, and **resume** only a pause the server imposed, within existing local consent;
- **revoke** a contribution;
- **request setup**, which creates a notice on the machine and never a native prompt.

It can never enable local access, write access or a scraper the operator did
not turn on. A dismissed "new scraper" announcement never grants consent.
Commands carry durable ids and ordered generations and are acknowledged
idempotently over the sidecar's polling.

## Withdrawal

Disabling a source withdraws only its own contribution (identified by device,
scraper, origin or account, and generation). The server deletes the matching
secret cache, records a tombstone so a late ingest cannot bring it back, and
re-enabling uses a newer generation. Other sidecars' contributions, manual and
file-based credentials, and historical `usage_events` are untouched; collection
stops only when no eligible credential is left. A machine that turns a source
off while offline shows **Withdrawal pending** until it reports; a revocation
from the server takes effect there immediately.

A local checkbox does not retroactively revoke a credential the server already
holds; revoke it from Fleet.

## Windows Credential Manager

The Windows Credential Manager entries for GitHub (`git:https://github.com`)
and Antigravity (`gemini:antigravity`) need no macOS-style consent and keep
collecting. They still have Fleet lifecycle rows, so pause, resume, revoke and
replay protection apply. They are not part of the local Keychain opt-ins, and
reconnecting a revoked one means pairing the machine again.

## Related

- [`sidecar.md`](sidecar.md): the sidecar.
- [`collectors/antigravity.md`](collectors/antigravity.md): Antigravity's macOS Keychain scraper.
- [`collectors/claude.md`](collectors/claude.md): Claude Code and Desktop sources.
