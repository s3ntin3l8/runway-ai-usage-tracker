# Offline verification of a Claude cloud diagnostic (#669)

Use `python -m scripts.verify_claude_cloud` to validate one sanitized diagnostic
report and record its immutable build context. This prepares evidence for the
bounded live verification in #908; it does not deploy, select a credential,
make a provider request, refresh a login, or enable cloud collection. The script
uses only the Python standard library and reads stdin. It does not import the
application, access its database, or read environment credentials.

The diagnostic endpoint is delivered separately in #908. This evaluator supports
its version-1 and explicit version-2 reports; an unsupported report version or additional field fails closed
until reviewed. It can be developed and tested before that endpoint is deployed.

## Before a live attempt

Do not run the provider diagnostic until all of these are recorded:

1. The reviewed source commit, passing CI (including both image-security audits),
   and immutable Runway image index digest. A mutable `edge` or version tag is not
   sufficient. Keep #909 open while the database freshness outage blocks audits.
2. The selected deployment, its current and proposed immutable image digests, and
   the existing deployment-specific rollback procedure. Do not substitute a new
   deployment or invent its update command from this document.
3. Explicit deployment approval for that concrete build, followed by confirmation
   that the deployed build matches it. This document and a synthetic test pass do
   not authorize production deployment.
4. One eligible exact account/source pair in the running server's cache. Review
   the selection privately in the authenticated operator session; never copy
   account, organization, device, scraper-origin or session identifiers into the
   evidence envelope, tickets, command arguments or logs. A metadata record alone
   does not prove that a cached access credential is present or eligible.
5. A readiness report for one operator-triggered POST, including the configured
   history-page budget (1–3), source selection, deadline and stop conditions.
   Confirm authorization before the live attempt. GET/status polling is inert.
   No native lookup, refresh, fallback sweep or automatic retry is part of it.

The chosen production deployment is already recorded in the investigation.
`selected-production` below is a fixed disclosure-safe label, not proof that an
input came from that deployment. Actual deployment and artifact binding must be
verified separately through the existing operator/release procedures.

## Evidence input and local command

Prepare exactly one UTF-8 JSON object on stdin with these fields:

| Field | Required value |
| --- | --- |
| `format_version` | Literal integer `1`. |
| `source_commit` | Full, lowercase 40-hex source commit of the deployed build. |
| `image` | `ghcr.io/s3ntin3l8/runway@sha256:` followed by a lowercase 64-hex immutable image digest. |
| `observed_at` | UTC time of the approved attempt, `YYYY-MM-DDTHH:MM:SSZ`. |
| `deployment` | `selected-production` or `isolated-test`. |
| `report` | The entire sanitized v1 or explicitly requested v2 diagnostic response, without extra fields. |

The input contains **no request body**, admin credential, account/source/session
ID, raw provider response, transcript, header, cursor or URL. Never obtain an
input by saving raw cloud-history responses. Do not use `curl -v`, shell tracing,
or command-line credentials while obtaining the already-sanitized diagnostic
through the approved operator channel. The evaluator itself has no HTTP client.

For an existing sanitized envelope, run locally with owner-only output:

```sh
umask 077
python -m scripts.verify_claude_cloud < evidence-input.json > evidence.json
```

An approved client may instead pipe the envelope directly to the evaluator, so
only validated output is retained. Input is capped at 256 KiB before decoding;
duplicate keys, unknown fields at every depth, nonstandard JSON constants,
unsupported versions, mutable image tags and noninteger/negative/oversized
counters are rejected. Errors use fixed categories without echoing input or
exceptions. The CLI accepts no arguments and performs no file/network discovery.

Operator-supplied commit/digest/time fields are checked for shape only. The tool
cannot authenticate the report, verify signatures, bind the image to the commit,
check deployment identity, or prove freshness. Replaying a valid old envelope
still validates its shape; it is not evidence of a new live attempt.

## Interpret the result

Exit **0**, `status: sample_observed`, means a complete bounded history sample
with verified holder/organization flags, consistent request/page/event counts,
no sequence anomalies, and usable usage observations at exactly one location.
It is permission to inspect that sample's contract and accounting evidence, not
a live-acceptance pass. The session list may still be limited to the first 20
sessions; completeness refers only to the selected history page chain. No
ordering, account-wide completeness, retention, product coverage or parity is
inferred.

Exit **2**, `status: stop`, means stop the live sequence and investigate. It never
requests another attempt, rotates credentials, chooses another source, or hides
a rejection. A valid report remains in the output as already-sanitized evidence;
invalid input produces only a fixed failure envelope.

| Reason | Next action |
| --- | --- |
| `invalid_evidence` | Check the supported report contract and private input preparation; do not paste rejected input into logs or issues. |
| `input_limit_exceeded` | Stop and check why the sanitized envelope exceeds 256 KiB; do not save raw responses or increase the bound to bypass investigation. |
| `probe_not_complete` | Inspect the fixed probe outcome; stop on rejection, timeout, unavailable source, unsupported shape or partial history. |
| `sample_requires_review` | Investigate missing identity/completeness flags or inconsistent sample bounds. |
| `sequence_requires_review` | Investigate invalid, duplicate, missing or out-of-order sequence evidence. |
| `usage_requires_review` | Investigate absent/incomplete usage, multiple envelope locations, subagent scope, conflicting records, errors or decreasing cumulative observations. Never combine locations or sum result snapshots. |

Every output sets `provenance_verified`, `live_acceptance_passed`,
`cloud_contract_verified` and `ready_for_ingestion` to **false**. Usage observations
remain separate and are never converted to consumed totals. A successful sample
still requires manual contract/envelope analysis, resume/reset/subagent/error
accounting comparisons, durable checkpoint design, overlap reconciliation, and
coverage evidence before a collector can be enabled. Retain local collection.

Closing the output pipe exits 2 without printing a traceback. A failed output
write leaves no complete evidence artifact; obtain a complete local validation
result before treating its status as usable.

## Delivery and acceptance boundaries

Retain the validated JSON and links to immutable source, image and CI evidence in
an owner-only evidence directory. Publish only the reviewed sanitized evidence;
keep private source selection outside it. If the POST or deployment behaves
unexpectedly, stop and use the existing rollback procedure as appropriate. A
read-only report has no ingestion/credential changes to undo.

No live execution is performed by this tooling milestone. #669 remains open.
#526, #750, #773, #767 and #781 retain their separate native, policy-preserving
renewal and signed-update acceptance gates. Real-account access/rotation requires
an isolated environment, readiness report and separate explicit approval.

## First production attempt: stopped before provider access

On 2026-10-24 at 20:42:08 UTC, the approved diagnostic on source
`4d2862933a7e07d5d5eb115d52c5fa99c909dd7b`, image
`ghcr.io/s3ntin3l8/runway@sha256:f2749ad276c2a34a91604403ca5d4991da3512a21d9e99a5a2cb7f9e27524f71`,
returned `unsupported_source` at the source stage, with zero provider requests.
The offline evaluator accepted the sanitized report's shape and returned `stop`;
all acceptance, contract, ingestion and provenance flags remained false.
Deployment health and the online backup's integrity and owner-only permissions
were verified. No provider retry, native lookup, refresh, rotation or activation
was performed.

The selected live Code file credential carried the sidecar's no-refresh marker.
A regression through actual sidecar egress reproduced the diagnostic rejecting
`oauth_read_only: "1"` before examining Code provenance. The fix is tracked in
[#933](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/933).
The marker does not itself identify Desktop OAuth; Desktop exclusion must use
source provenance, while the server's no-refresh restriction remains enforced.

Sanitized results and remaining acceptance gaps are recorded on
[#669](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/669#issuecomment-6389317097).
The immutable release passed
[main CI](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40128555610).
This source-validation result establishes no provider API/accounting/coverage
contract. The one-shot approval was consumed. A further attempt requires the
reviewed fix, a verified release image and a new concrete readiness/approval step.

## Retry after #935: producer origin format

After #935 passed review and all CI, source
`b764a894afe547a1e182c724020fb4a34c30d526`, image
`ghcr.io/s3ntin3l8/runway@sha256:6d843ad9a8c7f9362ee68c2a3097991e0897065302147026245cfdebce284aba`,
was deployed with another verified owner-only SQLite backup. The exact packaged
source matched the reviewed files; both release-image architecture audits,
signing and publication passed in
[run 40132425773](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40132425773).
The authorized diagnostic at 2026-10-24 21:16:00 UTC again returned
`unsupported_source` at the source stage, with zero provider requests.
The validated evidence retained all acceptance/contract/ingestion flags as false.

A metadata-only read of the exact selected source established that its origin
uses `path:`, has a recognized Code filename, has no fingerprint suffix, is not
an explicit configuration source, and is classified by inventory as Claude Code.
The sidecar token-file producer emits `path:<resolved filename>`; inventory
supports both `path:` and `file:`. The diagnostic still accepted only `file:`.
Thus #935 addressed the independent read-only-marker rejection, while its
fixtures missed the producer's origin spelling. This remaining rejection is
tracked in [#938](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/938).
No actual path, account/source identifier or credential value is retained here.

Regressions for both recognized Code filenames using `path:` failed against
that release. Accepting both file-origin spellings fixes those offline cases;
an unrelated filename continues to stop before provider access. This does not
prove live provider access or any other #669 acceptance requirement. The retry
was consumed; no provider retry, native lookup, refresh or activation followed.

## Explicit version-2 structural evidence (#941)

The request may include the strict integer `report_version: 2`. Omitting it
returns the unchanged v1 field set. V2 has `format_version: 2` and adds
`structure: {events, locations}`; envelope `format_version` stays 1. Values other
than integers 1 and 2, including booleans and strings, fail closed.

There is exactly one row per fixed path: `event` (the root), `data`, `content`,
`message`, `payload`, `event.event`, `data.message`, `content.message`, and
`event.event.message`. Paths traverse dictionaries only. Strings remain strings,
including strings containing JSON, and arrays are counted without visiting
items. Missing paths have their own type counter; null and boolean are distinct.

Each row has fixed-key `types`, `categories` and `fields` histograms. Categories
count the allowlisted `type` values assistant/result/user/system/tool_result/
tool_use/stream_event/message/text/thinking. Arbitrary strings become `other`;
missing/non-string type fields and non-object nodes have separate categories.
The presence fields are type/event/message/data/content/payload/usage/modelUsage/
role; only presence is counted, without returning any value. Every row has one
type and one category count per event. There are no dynamic keys, paths, category
strings, identifiers, message bodies or decoded JSON strings in this output.

The API response model and standalone offline verifier validate every key, integer, bound, histogram
total and parent/child presence relation against `history_events` (maximum 150).
It runs with `python -I -S` without application/site-package imports. Source
change, timeout or unavailable authority clears structural observations along
with the prior usage evidence, retaining the requested report version.

These counters distinguish sampled categories and container shapes; they never
establish zero usage, a validated usage wrapper, accounting parity, coverage or
permission to ingest. Existing acceptance flags remain false, and incomplete
history or missing usage observations still stop evaluation. V2 makes no extra
provider call and changes no request/page/time/byte/source/consent boundary.
