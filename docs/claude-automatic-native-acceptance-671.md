# Automatic Claude connections: native acceptance (#671)

Observed 2026-10-18 (Europe/Berlin). The latest accepted source-runtime run
used revision `3e2a744e` on macOS 26.6.2
(25G83), arm64, Chrome 155.0.8059.80 and Python 3.14.7.
See the [sanitized evidence](claude-automatic-native-acceptance-671.json).

## Result

Both subscription quotas and Console credits connected without pasted cookies.
The actual sidecar cookie rules discovered one pending candidate for each
provider. HMAC-authenticated manifest and metric ingestion both returned 200.
Production server collectors then verified the live upstream sessions,
promoted their distinct source identities, and the background poller persisted
three valid subscription cards and one valid Console balance card. After a
resolved-session heartbeat, second poll and collector synchronization, two
resolved accounts remained active. Both providers retained `data_source=web` and `input_source=sidecar`;
neither had a remaining pending source.

The accepted run made exactly one `Chrome Safe Storage` lookup, shared by
both session discoveries through the process cache. A guard rejected any
additional Keychain service or repeated lookup. Desktop credentials were
explicitly opted out; no Desktop or Claude Code Keychain lookup was made in
this accepted run. Ordinary Chrome sign-in was already present.

## Method and limits

- Created a temporary Runway configuration directory, database, encryption key,
  ingest key and admin key. Disabled inherited Claude OAuth/session environment
  credentials and Desktop discovery. The temporary directory was deleted when
  the acceptance process ended.
- Used the shipped embedded registry and `GenericCollector.collect_provider`
  with its cookie rules. This limited the run to the intended browser sources,
  avoiding unrelated CLI credential rules. No cookie was copied, pasted or
  supplied as a configuration override.
- Sent the resulting cards through the production manifest and ingest routes
  using the production HMAC scheme. HTTP transport to these local routes was
  in-process ASGI; upstream verification used real HTTPS with TLS verification
  and the production collector headers. This was not a mocked upstream test.
- After promotion, fetched production fleet configuration, rediscovered both
  sessions using its exact-source account hints, and posted a second signed
  manifest with no pending entries and both resolved health observations.
  Checked both durable sources and cache accounts survived, then uploaded the
  rediscovered token cards through production ingestion. Both requests returned
  200. This explicitly tests the post-verification heartbeat.
- Ran the production collector manager and background poller twice, including
  forced source/account reconciliation. Checked persisted `LatestUsage` rows,
  resolved `CredentialSource` rows, active account counts and pending cache
  state, emitting only counts and result flags.
- Retained no cookies, Keychain secrets, HMAC/admin/encryption keys, account
  names, organization identifiers, amounts, or raw responses in this report.

This verifies native source-runtime discovery through server persistence. It
does not claim installed/frozen-binary, tray, Docker/network transport, or
native ambiguous-organization UI acceptance. Organization picker, permission,
source-switch and race scenarios also have synthetic integration tests. The
remaining physical/source-specific cases stay in #672; Code Keychain layout
discovery stays in #673 and OAuth renewal/write-back in #526.

## Failures found and corrected before acceptance

Earlier controlled runs were not counted as acceptance. The driver first
failed the application's requirement for an explicit encryption key when an
admin key is set; it was corrected to generate an ephemeral encryption key.
Subsequent runs exposed a production cache-reconciliation defect: moving a
source to its already-current account removed the compatibility-cache entry,
leaving zero active collectors after polling. A regression now covers two
providers through manager collection, persistence, resynchronization and a
second poll. The accepted run retained both accounts.

Independent review also required binding verification to the exact credential
generation and selected organization through promotion and poller persistence.
Tests interleave replacement, removal, reassignment and organization changes
after verification to ensure stale results are discarded.

During an earlier focused unit-test run, a legacy discovery mock allowed an
unintended Desktop Keychain attempt that returned unavailable. Its exact count
was not captured, and it is not native acceptance evidence. Affected fixtures
now use disposable homes and block real `security` invocations by default.

## PR review: resolved-session heartbeat regression

Hermes found that source retirement used the pending-only origin map. A
verified or operator-tagged source could be removed on the next manifest,
even while still reported. Earlier two-poll acceptance did not exercise that
next sidecar manifest and therefore missed the defect. The latest native run
above adds rediscovery and an observation-only resolved heartbeat before the
second poll, and verifies both sources survive.

Retirement now retains origins from both pending entries and resolved health
observations. Cache and preference eviction waits until the database commit
succeeds. Since an empty scan can also mean denied or unavailable store access,
retirement additionally requires positive fingerprinted replacement evidence
from the same browser/profile/store in a completed scan. Empty scans preserve
last-good sources. Authoritative empty-store/logout proof remains tracked in
#672; this report does not claim automatic logout detection.

The same review identified two Linux/Windows CI failures caused by a test
using the real host home and OS. Its browser fixtures now explicitly select
Darwin and a temporary home, and assert the exact healthy Edge store. Native
Windows automatic-session acceptance remains separate in #675.

## Repository verification

The final combined `make lint && make test && make web-test` gate passed:
4,729 Python tests passed (41 skipped) and 1,185 frontend tests passed.
Ruff, formatting, mypy and dependency auditing passed. The production frontend
build and generated-registry consistency tests passed. Independent whole-branch
review approved the source guards, account-switch compatibility and organization
picker after its zero-eligible-organization state was corrected.

## Issue boundary

The feature and completed inventory/investigation/fallback scope belong to
#671, #663 and #666. Their PR closing references apply only after combined
verification and review. Remaining native stores/access cases (#672), Claude
Code discovery (#673), renewal (#526), Copilot Keychain (#534) and Cowork
ingestion/deployment acceptance (#656) remain open independently.
