# Claude native OAuth evidence (#679 / #673)

Observed 2026-10-19 on macOS 26.6.2 (25G83), arm64. No production configuration,
credential, ACL or DNS changes were made.

## Investigation evidence

An explicitly authorized, read-only Desktop probe retrieved `Claude Safe Storage`
and decrypted both `oauth:tokenCache` (six entries) and `oauth:tokenCacheV2` (four entries).
Nine distinct access-token candidates were found; eight were inspected by that bounded probe.
Four tested tokens, including two from V2, returned HTTP 200 for both holder profile
and quota. Each returned holder UUID matched its cache slot binding. Five-hour and
seven-day quota windows were present. Two expired tokens were skipped without refresh;
one token returned profile 200 / quota 403, and another returned profile 403.
These are credential/API observations, not implementation or ingestion acceptance.
No token, refresh secret, account identifier, quota amount or raw response is retained.

The default Claude Code Keychain entry was readable JSON, associated with the current
macOS user, but had empty access/refresh fields and a past numeric expiry. The checked
shell and sidecar configuration named no extra login directory, and standard credential
files were absent. The service derived from an explicit default configuration directory
was absent. This bounded inventory is not proof of absence from every possible directory.

## Implementation acceptance

Synthetic tests cover both caches, slot separation, V2 tombstones, deduplication,
rotation, missing/unsupported fields, encryption failures, opt-out, Keychain failure
suppression, account/service derivation, signed ingestion, multiple account promotion,
profile/UUID/quota failures, nonfinite/malformed quota, source-generation races and
operator-assignment preservation. They use temporary homes and mocked credential stores.

The final accepted native source-runtime run used the production Desktop discovery
rule, a temporary Runway configuration directory/database, generated ephemeral ingest,
admin and encryption keys, and real upstream HTTPS with TLS verification. The local
signed endpoints used in-process ASGI transport. Its single Safe Storage lookup
succeeded in 13.58 seconds. The key remained cached through rediscovery.

Seven candidates were discovered. Four sources passed holder/profile and quota
verification; three remained unpromoted and are not counted as usable. Both signed
manifest and metric requests returned HTTP 200, before and after rediscovery.
The production background poller persisted three quota cards for one account on both
polls, with `data_source=api` and `input_source=sidecar`. All four resolved source
identities survived the second manifest and rediscovery heartbeat. No token was pasted,
refreshed, or written back. The disposable configuration/database was removed afterward.
See [sanitized counts](claude-native-oauth-acceptance-679.json).

Earlier attempts are not counted as acceptance: one timed out; another reached both
polls but its report driver incorrectly read provenance as database columns rather than
from `card_json`. The corrected driver passed a synthetic end-to-end preflight before
the final user-attended run. No credential-store retries occurred within any run.

Final verification: `make lint && make test && make web-test` passed, including
**4,799 Python tests (41 skipped)** and **1,186 frontend tests**. The production SPA
build, generated registry consistency and four frozen spec import/syntax checks passed.
Secret scanning passed after updating existing baseline line offsets and its timestamp;
no detections were added or removed. Independent
review identified account-switch and malformed-quota gaps; regression tests failed
before the fixes and passed afterward, followed by the full gate.

After the user completed normal Claude Code login, the new Code Keychain rule
also passed production source-runtime acceptance. The initial lookup and rediscovery
both succeeded in 0.02 seconds, each scoped to the current macOS Keychain account.
One source passed holder and quota verification; both signed manifest/ingest pairs
returned HTTP 200. Three API/sidecar quota cards persisted for one account on both
polls, and the resolved heartbeat retained the source. The Code helper rereads the
credential on each cycle, so this run made two read-only lookups. No refresh secret
was forwarded, and Runway performed no login, refresh or credential write.

Desktop source-runtime acceptance satisfies #679; Code discovery and available native
login acceptance satisfy #673. Custom-directory and secure-storage override naming,
Unicode normalization and multi-account isolation have synthetic coverage; this run
did not create artificial native logins in extra directories. Frozen binaries/tray, other
browser
stores and native organization-picker behavior are not established here; existing
#672 remains open independently. Renewal/write-back remains #526.


## CodeQL review corrections

CodeQL identified an import cycle in the Code decoder and classified the Desktop
slot-name digest as password hashing. The decoder now handles only its declared
JSON dot paths/identity alternatives without importing the sidecar engine. Desktop
slot identifiers use the repository's established domain-separated PBKDF2-HMAC
fingerprinting pattern. Generic diagnostic logs no longer include credential-derived
values or raw parse failures. No warning suppression was added.

The successful Desktop native capture above was taken at `534e6a30`. After the
fingerprinting correction, the final synthetic production ingestion/heartbeat/two-poll
driver passed. A fresh native Desktop lookup timed out and was stopped; it is not
counted as a new successful capture. The standalone Code decoder passed another native
signed-ingestion, rediscovery and two-poll run after these corrections. The independent
reviewer found no actionable issue in the corrections; 42 focused tests passed.

The final logging regression confirms that neither a credential value nor its origin
can leak through the pending-source diagnostic warning. The two remaining origin-bearing
warning messages were removed after CodeQL identified them.
