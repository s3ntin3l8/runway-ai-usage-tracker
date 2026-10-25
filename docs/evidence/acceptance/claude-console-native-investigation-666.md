# Claude Console cookie discovery: native macOS investigation (#666)

Investigated on **2026-10-18, Europe/Berlin** (2026-10-17 22:03:21 UTC).
Related: [#666](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/666),
[#663](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/663).

## Conclusion

Native Chrome Console-cookie discovery is feasible on this Mac. Chrome's
`Default` profile contained a `.platform.claude.com` `sessionKey`. One Chrome
Safe Storage Keychain lookup supplied the encryption secret; local decryption
passed strict padding and cookie-domain binding checks. Read-only organization
discovery and billing requests both returned HTTP 200. The billing payload
passed Runway's existing `parse_console_balance` parser.

This verifies this session's direct access to the upstream Console API. It does
not verify automatic sidecar ingestion, Runway account assignment, or fallback.
No automatic-discovery implementation or public interface was changed.

## Native environment and method

- macOS 26.6.2, build 25G83; `uname -m`: `arm64`.
- Chrome 155.0.8059.80 installed in `/Applications/Google Chrome.app`.
- Chrome Local State registered `Default` and `Profile 4`. Each had a `Cookies`
  store under `~/Library/Application Support/Google/Chrome/<profile>/Cookies`.
- Both SQLite databases opened with URI `mode=ro`, without copying browser
  data. Both reported cookie schema version 24.
- Selected only `sessionKey` records on Claude/Anthropic domains. Console and
  Claude.ai cookies remained separate. Only the Console candidate was decrypted
  and used for HTTPS verification; the quota cookie was not decrypted.
- Used an ephemeral Python process with installed `cryptography` and `httpx`.
  Secrets and response bodies remained in process memory; only selected
  metadata and result flags were emitted. No raw cookie database, key, cookie,
  organization identifier, account name, or billing response was persisted.

The adjacent [sanitized evidence](claude-console-native-investigation-666.json)
records the observation timestamp, expiry metadata, lookup outcome and HTTP
results. Profile directory names identify sources, not account identities.

## Results by stage

| Source | Discovery / store access | Decryption | Organization identification | Billing verification |
| --- | --- | --- | --- | --- |
| Chrome / Default / Console | Readable; `.platform.claude.com`, `sessionKey`, `v10`, schema 24 | Passed PKCS#7 padding and SHA-256 host binding; nonempty UTF-8 value | HTTP 200; valid list with one API organization, anonymized as `organization-1` | HTTP 200; existing collector parser accepted payload |
| Chrome / Default / subscription | Readable; `.claude.ai`, `sessionKey`, `v10`, schema 24 | Not attempted | Not attempted | Not attempted |
| Chrome / Profile 4 | Readable; no matching Claude/Anthropic `sessionKey` | No candidate | Not attempted | Not attempted |

Both discovered cookies had empty plaintext columns. Console expiry metadata:
2026-10-24 13:52:20 UTC; subscription expiry metadata: 2026-11-14 15:01:59 UTC.
Neither was expired at observation time. These are stored expiry timestamps,
not guarantees of continued access: revocation or permission changes may end
access earlier. Expired-session behavior was not exercised.

The billing response contained purchased and promotional tranche arrays and a
promotion with `program_id=subscription_api_credit`. Only presence and parser
acceptance were recorded; amounts, organization IDs and response bodies were
not retained. This does not infer token consumption or spending.

### Keychain access and format compatibility

Exactly one `security find-generic-password -s "Chrome Safe Storage" -w` call
was made with a five-second subprocess timeout. It returned status 0 in 0.015
seconds. Captured output was used only in memory. No Desktop Keychain item was
queried, and no lookup was retried.

This item supplied Chrome's encryption secret, not a directly usable Console
session. The cookie database supplied the encrypted session. This bounded
investigation does **not** establish that no other Keychain item stores a
session: no general Keychain inventory was performed.

Decryption used PBKDF2-HMAC-SHA1 (salt `saltysalt`, 1003 iterations, 16-byte
key), AES-CBC (16-space IV), and strict PKCS#7 unpadding. For schema 24, the
first 32 decrypted bytes matched SHA-256 of the exact stored `host_key`,
including its leading dot. Removing that binding prefix produced the cookie.
Only native `v10` was verified; `v11`, newer formats and other Chrome versions
remain unverified.

The agent performed no UI interaction to complete the lookup; whether a prompt
appeared was not independently observed because macOS UI was not monitored.
Existing Keychain access policy may explain immediate success. Denied access,
a locked Keychain, SQLite locks and timeout behavior were not induced or
verified. Other users, Python binaries or a packaged sidecar may receive a
permission prompt or different access outcome. A future denial or timeout must
stop the attempt and suppress repeated prompts.

### Read-only upstream verification

Using the Console cookie only, the process made one GET each to:

1. `https://platform.claude.com/api/organizations`
2. `https://platform.claude.com/api/organizations/{organization UUID}/prepaid/credits`

HTTPS certificate verification remained enabled, redirects were disabled,
requests had a ten-second timeout, and no automatic retries were configured.
Organization UUIDs were validated before constructing the billing path. The
one discovered organization remained associated with the `Default` candidate
in memory. No identity was assigned to an existing Runway connection.

No 401, 403, 429, transport error or parsing failure occurred. Failure handling
was therefore not natively verified. Future evidence must distinguish 401
rejection, ambiguous 403 authentication/permission denial, rate limiting,
transport errors and schema failures rather than treating every failure as an
invalid credential.

## Existing code gaps and smallest integration recommendation

Inspection at source revision `264c25ce` found:

- `BrowserCookieExtractor.get_all_paths()` hardcodes `Default`, `Profile 1`
  and `Profile 2`; it misses this Mac's `Profile 4`. Discover actual profile
  directories, cross-check Local State, and preserve browser/profile/store
  identity while avoiding duplicate legacy and Network store candidates.
- `decrypt_chromium_cookie()` invokes Keychain per cookie, checks only the
  last padding byte, and decodes the whole plaintext without removing or
  validating schema-24 host binding. It cannot correctly return the verified
  schema-24 session through that path. This is a static finding; the helper
  was deliberately not invoked because that would repeat the Keychain lookup.
- The Desktop helper already demonstrates strict padding, schema-aware host
  binding and bounded key caching. Reuse the validation principles for Chrome
  without invoking `Claude Safe Storage`, coupling the credential types, or
  copying Desktop's automatic retry interval without a prompt policy.
- The generic extractor returns the first nonempty value, discarding profile
  identity. Discovery should return distinct candidates rather than equating
  successful decryption with a verified credential.

Keep `anthropic_console` credentials separate from subscription credentials.
Carry browser, profile and store provenance through sidecar upload, then use
server-side identity verification and the existing Console organization flow
before binding a candidate to an account/organization. Do not merge credentials
solely because both cookies are named `sessionKey` or share a token prefix.
Billing access to an organization alone does not establish a match to a saved
Runway account. Multiple accessible organizations need explicit selection or
an existing verified organization binding.

Coordinate candidate selection and verified fallback with #663: a rejected
candidate must not hide another valid candidate; account mismatches must not
cross-populate connections; transient failures must not discard a previously
verified source. Cache successful Chrome key access for a bounded process
interval and suppress repeat denied/timed-out lookups until an explicit user
retry. Never log subprocess output or exception text containing secrets.
Keep browser and Keychain access exclusively in the sidecar; the server
receives credentials and performs API validation only.

Before shipping discovery, cover rejected-first/valid-second candidates,
different accounts, multiple organizations, expired sessions, host-binding and
padding failures, unsupported formats, missing stores, denial/timeouts and
transient upstream failures. Those implementation and failure-mode checks are
separate from this successful native investigation and remain tracked by #666
and #663. No issue was closed or commented on by this investigation.
