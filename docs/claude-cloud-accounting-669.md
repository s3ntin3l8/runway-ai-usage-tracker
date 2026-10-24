# Claude cloud accounting investigation (#669)

The cloud collector is not enabled. The first implementation milestone is an
**offline audit of SDK-style usage records**, separate from a cloud API adapter.
The historical endpoint envelope, account/org binding, coverage, pagination,
sequence continuity and cloud/local deduplication still require revalidation.
Local collection remains enabled until coverage and accounting parity pass.

The [offline diagnostic verification guide](claude-cloud-verification-669.md)
prepares immutable-build evidence and explicit sample/stop criteria for the
source-pinned diagnostic in #908. It performs no deployment or live access.

## Accounting evidence

[Claude's SDK accounting documentation](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
was checked on 2026-10-24. It describes assistant output counts as placeholders,
deduplication by message ID, and separate turn/main-agent and cumulative/per-model
scopes. Resumes, resets, subagents, SDK/Code versions and error results affect
interpretation. SDK cost fields are estimates rather than billing authority.

Those documented SDK semantics are a hypothesis for #669's historical mismatch;
they do not prove the semantics of `/v1/code/sessions/{id}/events`. In particular,
never map its records into the existing JSONL ingestor without verifying the
actual envelope and accumulation boundaries.

## Offline audit

Run `.venv/bin/python -m scripts.audit_claude_usage` with SDK-style JSONL on stdin.
The tool performs no networking, credential discovery, app startup, or database
writes. Do not save raw cloud responses to obtain its input. A future verified
adapter must extract usage records in memory and discard message content.

The accepted shapes are TypeScript/CLI-style `assistant` records with nested
`message.id` and `message.usage`, and `result` records with `usage` and optional
`modelUsage`. This is not a Python SDK dataclass adapter or cloud envelope parser.
User, system and stream records are ignored; stream deltas and reset markers are
not interpreted. Subagent assistant records are excluded from the main-agent
input/cache aggregate, and that exclusion is reported.

Output contains fixed categories and numeric usage observations. It omits message,
session, account, tool and model identifiers, content, unknown properties and raw
exceptions. Repeated assistant IDs count once. Conflicting input/cache usage
invalidates the aggregate; there is no first/last-wins correction policy.
Output-only placeholder divergence is reported separately and preserves matching
input/cache. Missing,
noninteger, negative, boolean or oversized counters remain unknown. All four token
fields must be present; absent cache fields do not silently become zero.

Result `usage` and `modelUsage` observations remain separate. Only the last record's
observation is shown, without adding snapshots together or presenting either as a
verified session total. Missing final values clear the last observation. A decrease
is counted as evidence, not interpreted as a reset. Error results are flagged;
zero counters do not prove zero consumption.

`ready_for_ingestion` and `cloud_contract_verified` are always false. Exit 0 means
only that recognized observations were obtained. Exit 2 signals incomplete or
invalid evidence. Input is bounded to 64 MiB, 1 MiB per line and 100,000 nonblank
records. Invalid encoding, duplicate JSON keys and nonstandard constants are
rejected with sanitized diagnostics. A UTF-8 BOM is accepted only at the start
of the file. Success and failure reports share the same schema.

## Next gates

1. Revalidate the history interface with an eligible, account/org-bound access
   credential on the selected server, using bounded read-only requests. No refresh
   exchanges, native credential access or production ingestion.
2. Compare per-step input/cache, final output, turn results and per-model snapshots
   across resumes, resets, subagents and errors. Retain usage evidence only.
3. Establish stable overlap identity, corrections, bounded pagination/backfill and
   durable checkpoints. Test replay and partial-history behavior before ingestion.
4. Prove coverage separately for CLI Code, Desktop Code, remote Code, Cowork,
   ordinary Chat and mobile-only sessions. Retain local fallback where unproved.

This tool does not satisfy the live API or accounting-parity acceptance gates.
#669 remains open. Native write strategy (#781), signing continuity (#767), and
#773/#750/#526 acceptance remain separate.

## Selected-server preflight (2026-10-24)

The operator selected the production Runway deployment. Its container was healthy
and reachable through the existing SSH connection. Read-only database metadata
showed no durable Claude Code contribution, no durable Anthropic contribution,
and no enabled/nonarchived configured Anthropic access credential. The configured
Claude OAuth environment variable was absent. Twelve enabled source metadata
records advertised an OAuth token type; metadata does not prove that those
credentials are currently usable in the running application's in-memory cache.

No provider request or refresh exchange was made. No native lookup was attempted.
A separate process cannot reuse the application's legacy in-memory cache through
the existing credential persistence interface. Live verification therefore remains
pending a supported, source-pinned execution path in the running application or
an eligible existing credential in the normal durable store. These results are
not an API authorization rejection or evidence that the sidecar stopped collecting.

## Operator-triggered server diagnostic

`POST /api/v1/system/debug/claude-cloud` is an admin-only, rate-limited diagnostic
for one explicit cached source. GET is inert. The normal Host, Origin and admin
checks apply; there is no new permission, automatic execution or source discovery.
It accepts at most 8 KiB of JSON, rejects duplicate keys and unknown fields, and
never echoes request values in validation errors.

Request fields:

| Field | Meaning |
| --- | --- |
| `account_id` | Exact resolved account slot; `default` cannot be probed. |
| `source_id` | Exact cached source ID, supplied in the body rather than the URL. |
| `organization_id` | Optional UUID assertion, checked against the authenticated profile and any configured organization binding. |
| `session_id` | Optional safe session ID that must appear in the sampled list page. |
| `max_history_pages` | Literal integer 1–3; defaults to 2. |

The service reads the selected source from the running application's cache. It
never combines bundles or falls back to another source or a merged credential.
A source must have an enabled persisted source record and an active account.
Identity-pending sources and known-expired access credentials are not called.
Eligible origins are Claude credential files (`path:` from the sidecar producer or
`file:`, both retaining the recognized Code filename check), the existing Claude OAuth environment
source, explicit configuration, and enrolled Claude Code Keychain contributions.
Desktop OAuth sources are excluded from this Code-specific probe by their origin.
The `oauth_read_only: "1"` marker prevents server refresh; sidecar egress also adds
it to borrowed Code credentials. A read-only credential needs a recognized Code
file/environment origin or an enrolled Code Keychain contribution; the marker
alone never establishes source provenance. Unknown marker values fail closed.
A cached Keychain contribution also needs its current device, scraper revision,
consent generation and unpaused/unrevoked local permission. The server action
cannot authorize a local scraper.

The first upstream GET is the OAuth profile. Holder email/UUID and organization
UUID must be usable; holder identity must match the account slot, and any token,
operator or configured organization assertions must agree. Only then is the
organization-scoped session list requested. No identity, UUID or token is returned.
Authority and the exact credential bundle are re-read before/after requests and
before publication. Rechecks include the original origin, device, scraper and
consent generation; re-enabling with unchanged tokens cannot revive old work. Late results are discarded if the source changes, is disabled,
is withdrawn, or cannot be revalidated.

A read-only cache snapshot avoids unrelated credential reload or expiry cleanup.
Exact durable payloads are checked against that snapshot; every enrolled source,
including files, remains subject to device revocation. Metadata checks use a
separate SQLite read-only connection in a worker thread, with a 250 ms busy
timeout. Cancellation cannot turn late thread results into provider requests or
published evidence.

The attempt/response is limited to 30 seconds, at most five upstream GETs, 1 MiB
per response and 4 MiB total. Redirects, response compression, retries and refresh
exchanges are disabled. One attempt can run per worker; a competing attempt gets
`busy`. The endpoint allows two attempts per minute. Cancellation closes the
response stream and releases admission.

The probe samples only the **first 20 listed sessions** and one session's first
1–3 history pages of at most 50 events each. Without an explicit session, it uses
the first returned ID; no ordering or recency is assumed. An ID absent from that
list sample produces `sample_not_found`, not a nonexistent-session claim. Pagination
requires an explicit nullable `next_cursor`; cursor loops and unsupported shapes
stop the attempt. `session_list_complete` and `history_complete` describe these
specific page chains only. `sample_complete` does not establish retention,
product coverage, numeric continuity or accounting parity.

Numeric sequence strings and integers are normalized for duplicate/gap/order
counts, without returning sequence values. SDK-shaped objects at the event root,
`data`, and `content` locations are audited separately. Strings are not parsed as
embedded records. These locations are observations of an **unverified envelope**;
never combine their reports or promote them into consumption. Both
`cloud_contract_verified` and `ready_for_ingestion` remain false.

Reports contain fixed outcomes, booleans, numeric observations and the offline
audit's sanitized fields. No conversation text, credential, account/session/model
ID, cursor, request headers or raw provider error is returned or persisted by the
probe. It does not update collection health, source promotion, backoff, usage
records or renewal state. Access requests use only the selected access credential;
refresh prevention remains unchanged.

### Readiness and remaining acceptance

The implementation and synthetic tests do not prove the live cloud contract.
A reviewed build must first be deployed with explicit approval. Native credential
probes remain quarantined; this diagnostic uses the server's existing cache only.
Before live execution, record the immutable build, selected deployment and one
eligible account/source pair without publishing its identifiers. Run one bounded
POST, retain the sanitized report, and stop on rejection, timeout or unsupported
contract. No fallback sweep or token rotation is authorized by this diagnostic.

Then determine the actual envelope and accounting/version scopes from that
bounded evidence before expanding pagination or building ingestion. Retain local
collection and keep #669 open. No native acceptance tracker is closed by this work.
