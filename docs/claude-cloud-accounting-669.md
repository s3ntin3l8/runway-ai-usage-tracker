# Claude cloud accounting investigation (#669)

The cloud collector is not enabled. The first implementation milestone is an
**offline audit of SDK-style usage records**, separate from a cloud API adapter.
The historical endpoint envelope, account/org binding, coverage, pagination,
sequence continuity and cloud/local deduplication still require revalidation.
Local collection remains enabled until coverage and accounting parity pass.

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
