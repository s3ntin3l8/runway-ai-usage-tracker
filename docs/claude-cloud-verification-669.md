# Offline verification of a Claude cloud diagnostic (#669)

Use `python -m scripts.verify_claude_cloud` to validate one sanitized diagnostic
report and record its immutable build context. This prepares evidence for the
bounded live verification in #908; it does not deploy, select a credential,
make a provider request, refresh a login, or enable cloud collection. The script
uses only the Python standard library and reads stdin. It does not import the
application, access its database, or read environment credentials.

The diagnostic endpoint is delivered separately in #908. This evaluator supports
its version-1 report; a different report version or additional field fails closed
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
| `report` | The entire sanitized version-1 response from #908's diagnostic, without extra fields. |

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
