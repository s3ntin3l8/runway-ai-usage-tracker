# Claude cloud: one mapped Desktop Code fixture (2026-10-26)

**Result: input/cache parity passed for one exact fixture; full acceptance stopped.**
Cloud collection remains disabled and local collection remains the fallback.
#669 and #956 stay open. The missing correspondence tracked by #992 is established
for this fixture; [#993](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/993)
tracks the observed output-accounting gap.

## Immutable execution context

| Evidence | Value |
| --- | --- |
| Reviewed implementation | [PR #986](https://github.com/s3ntin3l8/runway-ai-usage-tracker/pull/986) |
| Deployed source | `5fa8a3d77bfec3aa9a2d12691215a6a1f573bd7c` |
| Immutable image | `ghcr.io/s3ntin3l8/runway@sha256:53615b2695e588437fb40552a50d84c6413eee5ec1566fedaa4bab99eb32310a` |
| Release run | [40340711348, attempt 1](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40340711348) |
| Both-architecture audit artifact | [12659287341](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/40340711348/artifacts/12659287341) |
| Artifact digest | `sha256:e57b885175458699f6825ca665ef6191cbf3e8e5f4d8878e171f162fb8ed13f8` |
| Final report time | `2026-10-26T15:17:42Z` |
| Deployment label | `selected-production` |

Both architecture audits passed under the existing policy; signing preceded exact
digest promotion. All 17 packaged source hashes matched. Network-isolated packaged
checks passed for omitted terminal cursors, durable resume, terminal no replay,
public report validation, the archive API and verified backup preservation.
These checks made no provider requests.

The operator verified the running image and health before each live action,
then checked that its container and image were unchanged afterward. A fresh owner-only SQLite backup (1,518,096,384 bytes) preserved the completed
first history job and both older stopped archives. Exact encrypted archival of the
first completed job succeeded before this new job began. None of those jobs was
resumed, reset or replayed. This attempt used a fresh random key, handle and budget.

## Exact fixture correspondence

The preceding arbitrary-history attempt exhausted 1,474 sessions and one history
of 659 events, but had no message overlap with the local Cowork fixture or any of
88 local Code transcripts. That nonmatch did not establish product exclusion.
See its [published evidence](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/669#issuecomment-6419134585).

Read-only static inspection of installed Claude Desktop 3.6556.2 then established
that its manifest serializer preserves `bridgeSessionIds` and its session lookup
uses those bindings. Its explicit comparison function strips either `cse_` or
`session_` before comparing the remaining identifier. The independently checked
module hashes are retained in the observations file; vendor code was not executed.

Using exactly that rule in memory, all 70 bridge links in the available Desktop
manifests matched IDs in the exhausted cloud list and associated local transcripts.
All 70 manifest account-subject and organization directory names matched the verified
profile. The manifests did not contain email fields; email equality was not assumed.
There was no matching bridge link for the preceding arbitrary history.

One stable, single-link Desktop Code manifest selected a bounded transcript with
47 assistant records and 25 distinct message IDs. Selection rejected unknown
prefixes, ambiguous matches, multiple bridge links, wrong account/org roots,
changed files and excessive inputs. It did not match titles, timestamps or list
position. The existing verification API received the exact linked session ID;
new profile checks and complete list traversal verified the target's membership.

The local transcript was reduced in memory with the new job's HMAC key. Only usage
and necessary scope observations entered the encrypted checkpoint. Raw IDs, keys,
titles and conversation contents are absent from these evidence files.

## Observed result

| Gate | Observation |
| --- | --- |
| Account and organization | Verified against the selected original account/org |
| Provider requests | 95 attempted GETs |
| Session list | Complete: 74 pages, 1,475 accessible sessions |
| Exact target history | Complete: 2 pages, 76 events |
| Sequence | 76 numeric; zero invalid, duplicate, gap or out-of-order observations |
| Assistant records | 47 records, 25 distinct messages, 22 duplicates |
| Same-message input/cache comparison | All 25 match; zero missing, additional, mismatched, conflicting or unusable messages |
| Output-counter comparison | Local/cloud observation sets differ for all 25 matching messages |
| Result observations | Two recognized zero-usage snapshots; no usable per-model observations |
| Execution kind | Unknown in the API report |
| Independent decision | Stop: accounting, coverage and provenance require review |

The read-only output comparison used the same job's encrypted `expected_local` and
`usage_records`; it required the exact matching message set. Neither side had
output variants within this fixture. Every local message had an output observation
above one; 24 cloud messages did. These are observations, not verified consumed totals.

The reducer preserves recognized zero result counters. Its existing report cannot
distinguish missing from empty `modelUsage`, so that wire detail remains unknown.
Static inspection of the installed client's bridge `sendResult` path found an
explicit constructor emitting result-shaped markers with zero usage and empty
`modelUsage`. This establishes a supported behavior of that installed client, not
the exact producer/version of these historical events or the cause of their output
differences.

[Anthropic's accounting documentation](https://code.claude.com/docs/en/agent-sdk/cost-tracking),
checked on 2026-10-26, distinguishes deduplicated input/cache observations from
assistant output placeholders. It describes result `usage` as turn/main-loop scoped
and `modelUsage` as including cumulative/subagent usage. Those SDK rules do not by
themselves prove the cloud history contract. Zero markers and missing final
accounting cannot establish zero consumption or whole-session totals.

## Reproduce the offline decision

The [JSON envelope](claude-cloud-mapped-fixture-669-20261026.json) is the complete
sanitized report without its transport handle. The separate
[observations](claude-cloud-mapped-fixture-669-20261026-observations.json) contain
only fixed categories, counts, booleans and build hashes; they are not additions to
the frozen verification schema.

```sh
python -m scripts.verify_claude_cloud \
  < docs/evidence/acceptance/claude-cloud-mapped-fixture-669-20261026.json
```

Expected exit status: **2**. Expected decision: `stop`, with exactly
`accounting_requires_review`, `coverage_requires_review` and
`provenance_requires_review`. An isolated standard-library invocation of the
source-pinned verifier independently reproduced the entire retained decision.
This command performs no network, credential, database or native operations.

## Remaining acceptance

This run proves one fixture's correspondence and input/cache parity. It does not
prove exact output, subagent/whole-tree accounting, execution provenance, arbitrary
CLI or remote Code coverage, Cowork, ordinary Chat, mobile-only coverage, deletion,
retention, offline activity or incremental backfill. Those remain #669/#956 gates.
New phone-only ordinary Chat and cloud-runner Code fixtures reported by the operator
have not been verified by this run. Opening a session on Desktop must not be used
to claim phone-only visibility.

[#993](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/993) requires tracing
result markers and finding a supported same-session output accounting source before
any output-parser or ingestion change. Unknown fields and scopes remain unknown.
No native credential lookup, refresh/rotation, fallback, model call or collector
activation occurred in this attempt. The operator-created mobile fixtures are
separate from this diagnostic. Native #526/#750/#773 and signing #767 acceptance
remain separate.
