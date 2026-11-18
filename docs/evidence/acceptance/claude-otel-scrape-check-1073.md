# Claude local scraping against OpenTelemetry `api_request` events (2026-11-17)

A one-week comparison of what the sidecar extracts from Claude Code transcripts with what Claude Code
itself reports through OpenTelemetry. It answers #1073: how much usage the local transcripts omit
(the side-model calls), which source can capture it, and whether local scraping is otherwise faithful.

Counts and sums only. No request ids, session ids, paths, titles, emails or message content appear
here. The raw telemetry stays on the owner's host.

## Method

- **Telemetry side:** a throwaway OTLP HTTP/JSON receiver on `127.0.0.1:4318` (`/v1/logs` only) kept
  `api_request` events and an allow-list of fields: `model`, `query_source`, the token fields
  (input, output, cache read, cache creation with the 5m/1h split), `cost_usd`, `request_id`,
  `session.id`, `speed`, `effort`, `event.timestamp`. User, account and organisation fields were never
  written. Claude Code was pointed at it by an `env` block in `~/.claude/settings.json`
  (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_LOGS_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`,
  `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_METRICS_EXPORTER=none`). A settings-file `env` block is enough;
  no command-line variables were needed.
- **Window:** 2026-11-09 12:41 to 2026-11-16 23:21 UTC. 23,628 events, every `request_id` unique.
  Only sessions started after the switch-on are covered.
- **Transcript side:** `parse_anthropic_events` (the real extractor) over every
  `~/.claude/projects/**/*.jsonl`, 23,553 events in the window. Cost: `compute_event_cost` against an
  in-memory database seeded by `seed_pricing_table`, on `origin/main` at `f2343d0c`.
- **Join key:** the transcript record's top-level `requestId` equals the telemetry `request_id`.
  The extractor does **not** carry it (see Findings).

## Result 1: the main loop is scraped faithfully

20,382 requests are on both sides (86% of telemetry; the rest is Result 2). All of them agree on the
model after normalisation.

| Field | Agree | Differ |
|---|---|---|
| input tokens | 20,382 | 0 |
| cache read | 20,382 | 0 |
| cache creation (total) | 20,382 | 0 |
| cache creation 1h and 5m split | 20,382 | 0 |
| output tokens | 20,374 | 8 |

The 8 output differences are all cases where the extractor holds less than telemetry (53 tokens in
total against 27,929): the extractor at that commit kept the first of several streamed transcript
records of one message, and that record can carry a partial count (fixed since in #1350, which keeps
the record with the largest output count). Over all transcripts, 53,825 messages have more than one
record, 3,867 of them disagree on `output_tokens`, and the first record is not the maximum in 3,749
(the last record is not the maximum in 22).

3,170 transcript requests with a request id have no telemetry (one further event has none). 3,167
are in sessions that had no telemetry at all (already running when it was switched on, 2026-11-09 and
2026-11-10 mostly), so they are not misses. The other 3 are in sessions that have telemetry and were
not investigated.

## Result 2: what the transcripts omit

3,246 telemetry requests are not in any transcript ($73.14). 3,243 of them are in sessions that have
transcripts (**$72.94, 5.1% of the $1,425.41 matched spend**); 3 are in sessions without one.
It is spread evenly over the week ($0 to $24 a day, no day above 11% of that day's matched cost
except 2026-11-11 at 20%, a low-use day).

| `query_source` | Models | Requests | `cost_usd` |
|---|---|---|---|
| `agent_summary` | Sonnet, Opus, Fable | 1,291 | $40.45 |
| `compact` | Sonnet, Opus, Fable | 19 | $11.93 |
| `prompt_suggestion` | Sonnet, Opus, Fable | 488 | $10.76 |
| `hook_prompt` | Haiku 5.5 | 67 | $7.73 |
| `away_summary` | Sonnet, Opus | 68 | $1.30 |
| `generate_session_title` | Haiku 5.5 | 1,257 | $0.65 |
| `web_search_tool`, `web_fetch_apply`, `rename_generate_name` | Haiku 5.5 | 54 | $0.09 |
| `repl_main_thread` | Sonnet, Opus | 2 | $0.23 |

- The Haiku calls #1073 asked about come to **$8.47 in the week; the title calls are $0.65**, in
  1,257 requests (about 4,800 prompt tokens each). The larger Haiku cost is `hook_prompt`.
- Most of the omitted spend is on the main models (summaries, compaction, suggestions), not Haiku.
- The 2 `repl_main_thread` requests and the 3 requests in transcript-less sessions are isolated cases
  whose cause was not investigated; the planned receiver drops `repl_main_thread` and `agent:*` by
  decision, since transcripts cover those sources.
- Runway's price of these requests from their tokens equals `cost_usd`, except `web_search_tool`
  ($0.011 against $0.071; not investigated).

## Result 3: cost agreement

Runway's price (transcript tokens) against Claude's `cost_usd`, matched requests:

| Model | Requests | Runway | `cost_usd` |
|---|---|---|---|
| Sonnet 5.5 | 13,673 | $696.42 | $705.79 |
| Opus 5.5 | 5,816 | $447.79 | $565.01 |
| Fable 5.2 | 893 | $147.82 | $154.61 |

Per request the ratio is exactly 1.000 at the 10th, 50th and 90th percentile for every model, also
for Opus requests above 100k and 200k prompt tokens. The totals differ because of a tail of requests
where `cost_usd` is 2 to 20 times Runway's price (72 on Opus and Sonnet: $134.58 against $7.98, plus a $6.79 remainder on Fable; tracked in #1352).

**Cause: the advisor tool.** Claude Code writes `message.usage.iterations`, a list of `message`
entries (the main call) and `advisor_message` entries (the advisor's own sub-inference). Only an
`advisor_message` entry has a `model` key (`iterations[].model`, read from the owner's local transcripts;
`message` entries have none, and the cloud source-shape capture did not record it). 82 matched requests have an `advisor_message` entry (79 on `claude-fable-5-2`, 3 on
`claude-fable-5-1`), with a combined `cost_usd` of $163.60. On all 82 the top-level `usage` equals
the sum of the `message` entries only, and the telemetry token fields equal the top-level `usage`,
so both sources omit the advisor's tokens and agree with each other while `cost_usd` includes them.
The gap to Claude's total is $126.59 on Opus and Sonnet plus $6.79 on Fable, **about $133, 9.4% of the
matched spend**. One advisor entry, for scale: 142,952 input tokens, 12,650 output tokens, no cache.
The transcripts hold these entries, so the extractor can capture them without telemetry.

## Findings and where they go

1. Local scraping of the main loop is accurate. The only token defect was the partial output count of
   streamed records, fixed in #1350 after this measurement.
2. The extractor reads `message.requestId`; real transcripts store `requestId` at the top level of
   the record. In production 0 of 23,553 events carry a request id, so every `event_id` is the bare
   message id. The test fixtures put `requestId` inside `message`, which hides it.
3. Advisor-tool usage is missing from local totals, about 9% of spend, and is present in the
   transcripts (`usage.iterations`).
4. Side calls are a stable 5% of spend and are available in telemetry with their `query_source`,
   `model`, tokens and `cost_usd`, joinable by request id.
5. The bridge-block approach from the first phase (7 of 33 sessions exact on cache counters) is
   weaker than telemetry in every respect and is not pursued (#1274 closed).

## Decision

Owner, 2026-11-18. Advisor usage is extracted from the transcripts (#1352). Side calls are captured
from OpenTelemetry by a loopback receiver in the sidecar, enabled by an opt-in setting that writes
the telemetry `env` block into `~/.claude/settings.json` (default off). Only events the transcripts
lack are kept (`repl_main_thread` and `agent:*` are dropped), the call type is stored in a new
`query_source` column, and cost is priced from tokens on the server. Issues: #1351 (request id on
events, the join key), #1364 (`query_source` column), #1365 (receiver), #1366 (opt-in setting), #1352
(advisor usage). The design discussion is closed in #1353.

## Reproduce

The comparison is a throwaway script (not committed) that joins on `request_id`, runs
`parse_anthropic_events`, prices both sides with `compute_event_cost`, and prints counts and sums.
The prototype receiver is under 40 lines (parse `/v1/logs`, keep `api_request`, drop everything not
on the allow-list). #1283 tracks turning the comparison into a committed `scripts/otel_scrape_check.py`
if the check should be repeatable.
