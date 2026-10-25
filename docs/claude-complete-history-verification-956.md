# Complete-history Claude verification (#956, parent #669)

Implementation: usage-only reduction, encrypted checkpoints, a bounded pagination worker,
and the explicit admin protocol are implemented. Synthetic verification is separate from
packaged and live acceptance; neither has passed for this protocol yet.

## Intent and evidence

The operator has repeatedly requested completing #669 verification. The approved delivery plan requires exhausting pagination, bounded backfill/checkpoints, accounting reconciliation, explicit provenance and retaining local collection until parity. This is the next necessary verification milestone, not collector activation.

The #955 production sample verified source/account/org and recognized payload assistant usage, but stopped at 100 events. It found duplicates, output-placeholder divergence and zero-valued result snapshots without usable modelUsage. Prefixes cannot establish complete-session accounting. Two local Cowork transcripts are available; same-input parsers agree on input/cache for 68 and20 messages, which does not establish cloud parity. Mobile-only fixtures are unavailable and remain unverified.

## Chosen design

Extend the Python server using its existing admin authentication, Host/CSRF protections, cached-source authority and encryption facilities. Leave the existing diagnostic's default behavior and request/response limits unchanged. Add a separate explicit verification protocol with start, step, status and cancel operations. Status and page loads never issue provider requests. No native access, refresh, fallback, model calls or production ingestion.

One durable verification job may exist at a time. Start records explicit operator intent for one account/source and optional target session. Step performs at most five GETs in30seconds and4MiB total, with1MiB per response. Each step verifies the OAuth profile and holder/org against both original source and job identity. A whole job is capped at512 attempted requests,256MiB,100,000 history records,20,000 listed sessions and30minutes. These are implementation choices, not provider guarantees. A limit produces an explicit incomplete stop; it never silently widens.

Exhaust the required session list page chain (20 records/page), then the selected session's history (50/page, ascending). An optional target must be found authoritatively in that list; no match is not a nonexistent-session claim until the list completes. Without a target, the first listed session is selected, without assuming ordering or recency. Check repeated cursors, duplicate list IDs, sequence normalization, duplicates, gaps and ordering. Never sweep other histories automatically.

Use a new forward-safe SQLModel table with one row, encrypted checkpoint state and an optimistic revision/lease. Require configured encryption; never use its legacy plaintext fallback. Checkpoint state contains necessary bound identities/cursors, budgets and usage-only records. It contains no credential bundle, refresh token, provider body, conversation, tool arguments, titles, model names or raw exception. Message and model identities become domain-separated job-scoped HMACs, retaining distinct model scopes for reset analysis; the job salt and identity metadata remain encrypted. A source digest binds the exact credential/authority snapshot across steps without persisting credentials.

Claim work atomically across threads/processes; commit an attempted-request intent and charge the budget before each GET. Persist processed page state and clear intent atomically before the next operation. A restart between committed batches can continue. An in-flight intent after interruption stops automatically; no replay/retry or budget reset. Timeout, denial, source/consent change, encryption failure, lost lease, corrupt/replayed checkpoint or unsupported contract stops without new provider calls. Source change discards publishable usage/identity evidence. Never report success from in-memory state that failed to persist. Cancellation between steps is immediate; an active step remains bounded and must not be represented as already cancelled until exclusion is obtained.

Keep only SDK assistant/result usage shapes at the demonstrated payload location. Retain the current SDK auditor's duplicate/conflict handling, subagent exclusion, output-placeholder exclusion and separate cumulative snapshots. Strip unknown data before checkpointing; do not decode strings or recurse into arrays. Malformed/missing fields remain distinct unknowns, not zeroes. Validate aggregate bounds. Completion of pagination never marks accounting verified.

Allow an optional bounded usage-only local fixture comparison: job-scoped HMAC message identities and strict numeric counters, with account binding. Compare matching input/cache values and report missing/conflicting overlap; do not add local and cloud counts together, or claim output consumption. Fixture labels are declared provenance, not proved execution location. Provider environment metadata may only produce fixed supported execution categories; viewing/client metadata never proves execution. Unknown remains unknown.

Return fixed, strictly typed statuses, counters and audit/comparison outcomes. Return an opaque job handle, never provider/account/source/message identifiers, cursors, salt or digests. All ingestion, full-coverage, provenance and accounting acceptance flags remain false. Mirror schemas in TypeScript, standalone evidence validation and API documentation. The existing v1/v2 formats remain compatible.

## Verification and delivery

Use real parser/state tests with synthetic HTTP only during development. Prove hard budgets and journal-before-network ordering, encryption-off/tamper rejection, restart, intent ambiguity, failed persistence, concurrent claims, cursor loops, duplicate/conflicting usage, source/consent changes, cancellation and output allowlisting. Exercise actual local/cloud comparison matching and mismatches with independently derived numbers. Existing source-bound probe and file/local collection regressions must pass.

Run make lint && make test && make web-lint && make web-test and frontend build as needed. Ready PR from current remote main, own diff review, automatic Hermes review, resolve feedback/security, no additional review request. Live execution only after immutable signed/audited build readiness with an explicit bounded target and budget. Never reuse #955's consumed diagnostic.

#669 stays open until actual product-category coverage, complete pagination/backfill, independent accounting/parity, provenance/deduplication, archive/deletion/retention/offline behavior and unattended source/fallback criteria are proved. Native acceptance remains separate. Available fixtures do not substitute for absent categories.
