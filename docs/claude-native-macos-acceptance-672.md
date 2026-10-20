# Scoped macOS Claude acceptance (#672)

Native capture: 2026-10-19, Europe/Berlin; report finalized 2026-10-20. This completes
acceptance for the user's installed **Chrome, Safari and Claude Desktop** sources.
Optional Chromium, Edge and Firefox acceptance is explicitly excluded and tracked
in [#683](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/683).
Windows remains #675; renewal/write-back remains #526.

## Build and isolation

macOS 26.6.2 (25G83), arm64; Chrome 155.0.8059.80; Safari 26.6.2. The tested
production PyInstaller macOS app contains extraction revision `82e92af6` and passed
ad-hoc signing/strict signature verification. Its launcher SHA-256 is
`ec7e3ad26e4298c69492d35d9e7838b149663e0172ca417df18c2ccfbb990265`;
the production-shaped ZIP SHA-256 is
`cbadd995253d9607d762086ac8506b107d47499352b4ca9f851dfe84f0935c10`.
This is a local acceptance build, not a notarization or release-attestation claim.

The user created temporary browser profiles and signed into subscription and
Console separately. An isolated home exposed only those profile directories and
Desktop's two approved credential files through aliases. Everyday browser stores,
CLI login files and chat history were excluded. Configuration, keys, logs and the
server/database were disposable. The app used actual loopback HTTP with production
HMAC signing, and the server used real upstream HTTPS with TLS verification.
Neither refresh nor credential write-back was enabled. The installed everyday app
was left running; only the test processes were stopped. Test browser profiles remain
present, signed out by the user.

A process guard allowed one real Safe Storage lookup per approved service and blocked
other Keychain services. The first run's isolated HOME also hid the native Keychain
search list: Chrome/Desktop were not counted as passing. A metadata-only comparison
found one search-list entry normally and zero in the test home. After explicit user
readiness, the corrected guard preserved normal HOME only for `/usr/bin/security`.
The app's browser/file discovery stayed isolated. No ACL or Keychain configuration
was changed. Secrets never entered evidence files.

Safari cookie paths initially returned macOS privacy denial. The user granted access
through normal System Settings; no protection was bypassed. Profile binding used only
metadata for the specifically named test profile, not browsing history.

## Successful native paths

| Source | Discovered | Holder and upstream access verified |
| --- | ---: | ---: |
| Chrome subscription + Console cookies | 2 | 2 |
| Safari subscription + Console cookies | 2 | 2 |
| Desktop session cookie | 1 | 1 |
| Desktop OAuth cache slots | 7 | 5 |

The two remaining OAuth candidates are not counted as usable. Permission and
rate-limit responses remain distinct from successful verification. The driver observed
403 and 429 responses during forced collection/verification cycles; it did not infer
logout or expiry from either. These forced resets are not evidence of ordinary retry
cadence; retry/fallback policy has separate automated coverage.

The packaged app sent seven successful signed manifests and seven successful signed
ingestions. The production server persisted **three subscription cards and one Console
card** on each of two polls. Every resolved source survived rediscovery and the
resolved-source heartbeat. Desktop cookie and OAuth identities stayed independent;
browser sources retained profile/store provenance. Successful OAuth does not substitute
for Desktop cookie acceptance: both passed independently here.

The user confirmed the test tray menu was visible with collection controls. Native
inspection exposed generic browser labels; a subsequent server/frontend display fix
names Chrome, Safari and Claude Desktop without leaking profile names or fingerprints.
That metadata-only follow-up is verified by source-list API/frontend regressions and a
packaged synthetic run; it does not claim a new native credential capture.
Automatic verification connected the available usable organizations; this run does
not claim an interactive ambiguous-organization picker test.

## Logout, access failures and limits

After native success, the user signed out of both services in both temporary browser
profiles and closed their windows. Safari's production parser and Chrome's read-only
cookie metadata showed zero live matching sessions in all four cases. This was a
successful empty read, not missing/inaccessible evidence. The former Safari session
values stayed in probe memory only; both upstream checks returned 403 after logout.
They were rejected, but **the exact revocation/expiry cause is unverified**.

A real signed empty manifest returned 200 and retained all source metadata and all
four last-good cards. Automatic empty-store retirement remains deliberately disabled;
positive same-store replacement evidence or explicit cleanup is still required.

| Failure case | Evidence class | Result |
| --- | --- | --- |
| Safari privacy denial | Native OS access before user grant | Inaccessible, never inferred logout |
| Locked cookie database | Disposable real SQLite exclusive lock | Inaccessible |
| Keychain denial | Injected exit status in packaged app, synthetic stores | `denied`, two guard invocations total across repeated scans |
| Keychain timeout | Injected blocking command in packaged app, synthetic stores | `timed_out`, two guard invocations total across repeated scans |
| Unsupported schema/cipher/binary format | Disposable fixtures in packaged app | Unsupported; working independent OAuth source retained |
| Local expiry | Expired fixtures in packaged app | Expired count recorded, values filtered; working sessions retained |

Denied/timed-out injected runs made **zero real Keychain reads**. Each process attempted
its two services once and reused the failure states through rediscovery/heartbeat;
a working independent Safari source continued through two polls. These verify the
packaged suppression paths, not physical user-denial/timeout dialogs. No live dialog
was deliberately forced by locking the login Keychain, changing ACLs or revoking an
everyday session. Missing, unsupported, denied and timed-out results stay separate.
Native expiry/revocation classifications beyond the observed post-logout 403 are not
claimed; automated 401/expiry/403/429 tests cover the distinct server policies.

## Repository verification

`make lint && make test && make web-test`, production SPA build, registry consistency,
four frozen-spec contract checks, secret scanning and full diff review are required
for delivery. Final counts and CI are recorded in the PR. Independent review found
and corrected unsupported SQLite layouts being misclassified as inaccessible,
protected Safari profile trees being silently omitted, and cookie client names being
hidden by a credentials-view formatter. Each fix has a failing-then-passing regression.

The secret baseline update only moves existing line numbers; no detections were added
or removed. No new public endpoint, ingestion field or database migration is introduced.
Internal scan reports contain status, counts and a one-way store identifier, never
credential values or native account/profile identifiers. Existing candidate-returning
APIs and conservative source retention remain compatible.

See [sanitized result counts](claude-native-macos-acceptance-672.json).
