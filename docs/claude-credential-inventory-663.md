# macOS Claude credential inventory (#663)

Inspected on 2026-10-18 (Europe/Berlin), source revision `264c25ce`.
This report separates source discovery, access, decryption and upstream
verification. Live checks began at 2026-10-17 22:12:57 UTC (00:12:57 CEST).
See the [sanitized native evidence](claude-credential-inventory-663.json).

The Console findings were published in
[#666](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/666#issuecomment-6241988981).
The inventory below concerns subscription authentication and related local
credential paths tracked in [#663](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/663).

## Source inventory

| Source | Native discovery / access | Decryption | Upstream verification |
| --- | --- | --- | --- |
| Claude Desktop Cookies + Claude Safe Storage | Cookies readable; schema 24, `.claude.ai` `sessionKey`, `v10`, empty plaintext column | One newly authorized Keychain lookup timed out after 5.004 seconds; no key available, no decryption attempted | Not attempted |
| Chrome Default Cookies + Chrome Safe Storage, subscription | Cookies readable; schema 24, `.claude.ai` `sessionKey`, `v10`, empty plaintext column | Key lookup succeeded in 0.016 seconds; strict padding and host binding passed | Organizations endpoint HTTP 403; quota access unverified |
| Chrome Default Cookies + Chrome Safe Storage, Console | Native decryption and billing verified separately in #666 | Passed strict padding and exact-host hash validation | Organizations and credits HTTP 200; balance parser accepted response |
| Chrome Profile 4 | Cookies readable; schema 24, no Claude/Anthropic `sessionKey` | No candidate | Not attempted |
| Chromium / Edge | Application Support roots exist, but no cookie stores found directly under their profile directories at inspection | No candidate | Not attempted |
| Firefox | Standard Profiles directory absent | Not applicable | Not attempted |
| Safari | Standard `~/Library/Cookies/Cookies.binarycookies` absent; no broader Safari storage search performed | Not applicable | Not attempted |
| Claude Code Keychain | `Claude Code-credentials` readable in 0.012 seconds; valid JSON | Expected access/refresh token fields empty or absent; numeric expiry in the past; no local identity in expected fields | No usable mapped token; no request attempted |
| Claude Code files | All standard macOS registry file paths absent (listed below); `CLAUDE_CONFIG_DIR` unset | Not applicable at these paths | Not attempted |

Desktop cookie expiry metadata: 2026-11-14 11:15:42 UTC. Chrome subscription
cookie expiry metadata: 2026-11-14 15:01:59 UTC. Both were in the future at
inspection; expiry metadata does not prove current authentication validity.
No cookie values, keys, account names, organization IDs or raw browser records
are included here.

The registry's macOS path expansion resolved to
`~/.claude/.credentials.json`,
`~/Library/Application Support/claude/.credentials.json`, and
`~/Library/Application Support/claude/oauth_creds.json`; each was absent.
`~/.config/claude/oauth_creds.json` was also absent. Additional login directories
from a running sidecar's local/fleet configuration were not loaded or examined.

## What the code supports, and what is missing

The normal claude.ai cookie has a Keychain-assisted path: the session is in the
browser or Desktop SQLite store, while the Safe Storage Keychain item supplies
the decryption secret. Claude Code's separate Keychain item supplies OAuth
credentials. Neither an encryption secret nor an OAuth token should be treated
as a browser `sessionKey`. This inspection does not prove the absence of other
direct session items in Keychain; no unrestricted Keychain dump was performed.

- Desktop extraction validates PKCS#7 padding and schema-24 host binding and
  caches successful and failed lookups for ten minutes. Native decryption and
  upstream access still need verification. Automatically retrying a denied
  lookup every ten minutes can repeat prompts; a deliberate retry policy is
  needed alongside the cache.
- Generic Chrome extraction lacks schema-24 host binding handling and strict
  padding checks. Its hardcoded profile list misses Profile 4, and its
  per-cookie Keychain lookup has no equivalent success/refusal cache.
  This is now a native reproduction: after the independent probe decrypted the
  subscription cookie, `decrypt_chromium_cookie()` returned no value for the
  same encrypted bytes and key. The helper's subprocess call was mocked with
  the already retrieved secret; this comparison did not repeat Keychain access.
- Desktop-first selection returns one cookie before browser discovery; a
  successfully decrypted but server-rejected Desktop cookie can hide a valid
  browser session. Current unit coverage explicitly asserts Desktop precedence,
  rather than validating rejected-Desktop/accepted-browser server failover.
- Cookie extraction loses browser/profile/store identity before candidate
  upload. Keep candidates separate and preserve their provenance and verified
  account identities through server selection and fallback.
- The registered Claude Code Keychain service is fixed. Config-directory-specific
  service names are not discovered by this rule. Expiry/renewal/write-back needs
  remain tracked in #526; this investigation must not refresh or rotate tokens.
- Copilot CLI Keychain support is separately tracked by #534 and is absent from
  the registered Keychain rules. No `copilot` executable was found on PATH; its
  native storage and format remain unverified. This is not evidence that no
  Copilot installation or Keychain item exists.

## Live verification and limits

The user explicitly authorized one new five-second lookup per relevant service,
including Desktop, overriding the earlier no-retry restriction for this bounded
run only. Exactly one lookup was made for each of `Claude Safe Storage`,
`Chrome Safe Storage` and `Claude Code-credentials`. The Desktop timeout was
not retried. The agent performed no UI interaction; prompt visibility was not
independently monitored. No stores were unlocked or ACLs changed.

The Chrome cookie was decrypted using PBKDF2-HMAC-SHA1 (1003 iterations,
`saltysalt`, 16-byte key), AES-CBC with a 16-space IV, strict PKCS#7 padding,
and SHA-256 validation of the exact `.claude.ai` host prefix for schema 24.
Only `v10` was verified. Key and cookie remained in process memory; they were
not stored in the evidence artifact.

One HTTPS GET to `https://claude.ai/api/organizations` returned 403. It used
the decrypted `sessionKey` with JSON Accept, Mozilla User-Agent, claude.ai
Origin and settings/usage Referer headers. TLS verification was enabled,
redirects disabled and timeout set to ten seconds. No retries were made.
Account and usage requests were skipped after the organization failure.
The response body and challenge headers were not retained, so this evidence
cannot distinguish an expired/revoked session from permission denial or a
browser/anti-automation challenge. A successful Console request from #666
does not establish subscription-cookie access. No account matching or
end-to-end Runway ingestion was verified.

The Claude Code item parsed as JSON, but the registry's expected
`claudeAiOauth.accessToken` and `refreshToken` fields contained no usable
values. The numeric `expiresAt` was past; `clientId` and expected local email
fields were also unavailable. No token refresh, credential write-back, or
OAuth API call was attempted. Other item layouts and service-name variants
remain unverified; this is not proof that no usable Claude Code login exists
elsewhere.

## Automated checks

Executed `python -m pytest tests/unit/test_claude_desktop_cookies.py
tests/unit/test_account_canonicalization.py -q --no-cov` using this checkout's
Python environment: **57 passed**. Credential-source unit tests use synthetic
stores and mocked Keychain calls. Their success is not native decryption,
upstream authentication, renewal, or end-to-end Runway ingestion evidence.

Keep denial, timeout, missing item, unsupported format, failed decryption,
upstream rejection, permission failure, rate limiting and transport failure
distinct. No further Keychain lookup is authorized by the completed three-call
run. Native locked-store, denied-access, expired-session, multiple-account and
server-fallback scenarios remain unverified; #663 tracks that unfinished work.

## Recommended order

1. Repair the reproduced generic Chrome schema-24 decryption gap and enumerate
   actual profiles; preserve distinct source candidates instead of returning
   the first decrypted cookie.
2. Carry those candidates through existing server identity verification and
   failover. Keep transient errors distinct from confirmed rejection, and do
   not assign credentials to accounts by a fleet-wide hint alone.
3. Resolve Desktop access through an explicit user-controlled action before
   attempting another native check; never turn the timeout into a poll loop.
4. Investigate the unusable Claude Code item and config-specific service
   discovery within #526 without rotating tokens during investigation.
5. For subscription HTTP 403 diagnosis, use a separately authorized native
   check that records only status, response content type and challenge flags,
   and matches the collector's request fingerprint. Do not declare the cookie
   invalid or quota collection working on the present evidence.

No production code was changed. No comments were posted to #663 and no issues
were closed during this inventory.

## Exact generic Chrome failure: controlled diagnosis

A subsequent controlled probe exercised the real
`scripts/sidecar.py::decrypt_chromium_cookie` with synthetic AES-CBC cookies
and a mocked Keychain response. It performed **zero real Keychain lookups**
and made no network requests. The plaintext structure was the only variable:

| Case | Existing helper | Strict host-aware decoding |
| --- | --- | --- |
| Schema 23, session bytes alone | Returned the expected synthetic value | Not needed |
| Schema 24, SHA-256(`.claude.ai`) + session bytes | `UnicodeDecodeError` at line 2273, then `None` | Returned the expected value; wrong host rejected |
| Schema 24, SHA-256(`.platform.claude.com`) + session bytes | Same exception and result | Returned the expected value; wrong host rejected |

The precise failing expression is
`decrypted[:-pad_len].decode("utf-8")`. AES decryption and key derivation can
both succeed, but this expression includes the binary 32-byte domain hash in
the UTF-8 decode. For these domains that produces `UnicodeDecodeError`, which
the broad exception handler hides behind a debug log and a `None` return.
The prior native comparison already showed the actual subscription cookie
decrypting correctly in the strict probe while the same helper returned no
value; the controlled probe now isolates the exact code failure. This does
not establish the exception details of the earlier native comparison, which
did not capture them.

The browser caller currently selects only `encrypted_value`. Neither cookie
schema version nor exact stored `host_key` reaches the decryptor. The repair
therefore needs to carry that metadata from SQLite, strictly validate padding,
verify and remove the host hash for schema >=24, and only then decode the
session. Blindly dropping 32 bytes would break older formats and skip domain
validation. Do not use UTF-8 `errors="ignore"` to hide the failure.

### What this means for complete automation

Both Console and subscription cookie bytes were recoverable from this Mac's
Chrome store using its Safe Storage secret, without manual cookie copying.
That establishes discovery/decryption feasibility for both, not an implemented
or verified fully automatic integration:

- Console billing access passed, but `anthropic_console.rules` is empty, so the
  current sidecar has no registered automatic Console discovery path.
- Subscription decryption passed, but its API request returned unexplained
  HTTP 403. The controlled decode reproduction does not explain that response:
  the native HTTP probe already used the correctly decoded session value.
- The current generic decoder fails before upload, omits Profile 4, and folds
  cookie provenance into `cookie:anthropic/session`. Candidate identity and
  verified fallback remain required for safe automatic account association.
- Desktop access remains blocked by its timed-out Keychain lookup, and the
  inspected Claude Code item offers no usable mapped OAuth token. Neither
  currently supplies a verified alternative subscription path in this run.

Acceptance for full automation is: with ordinary browser sign-in and any
necessary OS access grant, the sidecar discovers both session types, keeps
their source and account/organization associations separate, and the server
successfully verifies subscription usage and Console credits without pasted
cookies. Browser sign-in, session expiry and revocation still exist; automatic
discovery cannot guarantee access to a missing or rejected session.
