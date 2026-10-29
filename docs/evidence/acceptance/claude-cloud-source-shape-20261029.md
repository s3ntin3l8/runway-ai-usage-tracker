# Claude cloud-runner sessions: what the API actually returns (2026-10-29)

Structure-only capture of real `GET /v1/code/sessions` and `/events` responses, taken with the
repository owner's local Claude Code login (read-only GETs, 404 requests, no refresh). It records key
paths, JSON types, counts, scrubbed value patterns and token numbers. It records no ids, titles,
cwd/branch/repo values, tokens or message content. Machine-readable form:
[`claude-cloud-source-shape-20261029.json`](claude-cloud-source-shape-20261029.json).

This is the source the cloud event contract is written from. Earlier evidence
([`claude-cloud-mapped-fixture-669-20261026.md`](claude-cloud-mapped-fixture-669-20261026.md)) covered
one Desktop *bridge* session only; nothing here was verified for `anthropic_cloud` before.

## List rows

79 pages, 1,565 sessions: 21 `anthropic_cloud`, 1,544 `bridge`. `environment_kind` is a top-level key.
Page envelope: `next_cursor` on 77 pages, `next_cursor` + `resume_token` on 1, and **no `next_cursor`
key on the last page**; there is no `has_more`. Timestamps are ISO 8601 with fractional seconds.
All 21 cloud sessions have `status: archived` (so their usage is final); origins are
`android` 10, `desktop_app` 8, `web_claude_ai` 3.

Cloud-row fields that matter for Runway:

| Wire path | Present (of 21) | Use |
|---|---|---|
| `config.outcomes[].git_info.repo`, `.branches`, `.host`, `config.sources[].url` | 21 | repository (project) and branches |
| `external_metadata.current_branches` | 19 | branch |
| `config.model` | 21 | requested model |
| `external_metadata.last_served_model` | 18 | served model (top-level `last_served_model` is absent) |
| `external_metadata.usage.cost_usd` | 14 | Anthropic's own cost for the session |
| `external_metadata.usage.{input,output,cache_read,cache_write}_tokens` | 12 | session token totals |
| `last_event_at`, `updated_at`, `created_at` | 21 | timing |
| `title`, `user_message_count`, `participants`, `tags`, `relations` | 21 | not needed for usage |

Seven cloud rows have no usage block at all, and two more carry `cost_usd` but no token counters.

## Event histories

Three cloud sessions (smallest, median, largest by block size). Envelope `{data, next_cursor}`; each
record has `event_id`, `event_type`, `sequence_num` (string), `created_at` (all records), `source`,
and a `payload` holding the SDK record (`payload.timestamp` on about 60% of records).

Payload types seen: `assistant`, `user`, `result`, `system` (subtypes `init`, `task_started`,
`task_progress`, `task_notification`, `vcs_state_changed`, `compact_boundary`, ...), `tool_progress`,
`control_request`, `prompt_suggestion`, `rate_limit_event` and others.

`assistant` records carry what a local transcript carries: `message.id`, `message.model`,
`message.usage` including the 1h/5m cache split (`cache_creation.ephemeral_*`), `request_id`,
`session_id`, `timestamp`, `parent_tool_use_id` with `subagent_type` and `task_description` for
subagents, and tool names in `message.content[]`. About two records exist per `message.id`
(streaming); `stop_reason` is null in the stream. **A session uses more than one model**: the largest
has `claude-opus-5-5` (5,339 records), `claude-fable-5-1` (500) and `<synthetic>` (27).

Project context lives on `system` records, not on assistant records: `cwd` (163 in the largest
session), `original_cwd`, and `branch`. `cwd` has the form `/home/user/<repo>` (sometimes a
subdirectory), which is the same shape `project_label` already derives a project from.

`result` records carry per-model cumulative usage in `modelUsage[<model>]`: `inputTokens`,
`outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `costUSD`, `thinkingTokens`,
`canonicalModel`. The values grow monotonically across the results of one session. `system`
`task_progress` records carry `usage.total_tokens` and `tool_uses` (subagent tasks).

## Accounting

Two further captures (numbers and model names only, 312 read-only GETs in total; data in
[`claude-cloud-accounting-capture-20261029.json`](claude-cloud-accounting-capture-20261029.json))
keep **every** `result` record and compare against the owner's local transcripts. The first version of
this note compared the median session against result 5 of 13 and drew conclusions from that; those are
withdrawn and replaced by the following.

### Cloud-runner sessions (full histories of 5 sessions)

| Session | List block | Final `modelUsage` (sum over models) | Other readings |
|---|---|---|---|
| `c34272` | in 9,677, out 23,168, cr 9,599,834, cw 111,547, 3.3144 USD | identical on every field; `costUSD` sum 3.3144 | |
| `eeee6b` | in 6,964, out 71,181, cr 33,712,500, cw 695,707, 11.0964 USD | identical on every field; `costUSD` sum 11.0964 (Haiku 4.5 included: 353,899 cache writes, 0.447 USD) | assistant messages: cr 30.36M, cw 0.34M |
| `c34a2b` | in 1,226, out 5,034, cr 1,009,882, cw 59,901, 0.7829 USD | in 2,238, out 5,059, cr 1,201,570, cw 61,328, 0.8360 USD | block equals the sum of each result's per-turn `usage` plus the Haiku model's `modelUsage` (22+1,204; 5,017+17; cr and cw exactly) |
| `ce832f` | in 21,371, out 583,604, cr 269,603,543, cw 4,742,629, 93.257 USD | in 10,628, out 275,817, cr 135,210,766, 47.538 USD (monotonic over 34 results) | block is about 2x the final `modelUsage`, not a clean multiple; unexplained |
| `145eca` | `cost_usd` 11.4059 only | 12 results; `modelUsage` restarts between results (11.41, 1.74, 0.81, ...) | block equals result 1 alone; the 11 later results (7.74 USD) are not in it |

So the list block is **not one consistent quantity** across cloud sessions: it equals the final
`modelUsage` in two, a different construction in one, about twice it in one, and the first run only in
one. `modelUsage` is cumulative per process: it restarts when a process restarts (`145eca`).
`costUSD` is a **list-price estimate** (`costBasis: list`, `provider: firstParty`), not what was billed.
`modelUsage` carries a second model (Haiku 4.5) whose tokens are not in the assistant-message stream.

### Bridge (local CLI) sessions against the local transcripts

All 36 bridge rows with token counters were matched to a local transcript through shared assistant
`message.id`; 8 of them were compared in full (143 shared messages).
- **Per message, cloud history and local transcript agree exactly on input, cache read and cache
  write for all 143 shared messages.** Output agrees on 95 of 143: the cloud stream carries streaming
  placeholders (for example 2,253 against 4,200 in one session), the local transcript carries the final
  count.
- The list block's cache read and write equal the local per-message sums exactly in 5 of 8 sessions;
  in two it is 3.7% and 8% higher; in one the local transcript holds 560 messages (129M cache reads) while the cloud history has only
  10 and the block is of that order (1.18M cache reads against 0.78M in the 10 messages; cache writes
  equal).
- The block's output matches the local output (703 and 703; 10,324 and 10,324; 6,440 and 6,384; 10,985
  and 10,973; 13,687 and 13,624; 2,470 and 2,344; 5,113 and 4,200), not the placeholders.
- The block's input is larger than the local input (3,755 against 24, and similar): the difference is
  the side model's input, as in `c34a2b` (22 plus 1,204 Haiku). Local transcripts do not record
  side-model calls.

### What this supports

1. For input, cache read and cache write, per-message cloud events reproduce what local scraping
   records, exactly. No reconstruction of "how it is billed" is needed: Runway prices tokens per model
   with its own table, and `costUSD` is itself a list-price estimate.
2. Output tokens must come from somewhere other than the assistant stream: `result.usage` /
   `result.modelUsage` for cloud-runner sessions.
3. The list block is only a coarse cross-check, because its construction differs between sessions.
4. Side models (Haiku) appear in `modelUsage` only; they are a few percent of a session's cost.

## Not established yet

- Why `ce832f`'s block is about twice its final `modelUsage`, and why `145eca`'s block stops at the
  first run. Both suggest the block is accumulated by the runner in a way that depends on restarts.
- Whether `modelUsage` deltas per result, with restart detection, reproduce the per-message cache
  counts for every cloud session (they do for `c34272` and `c34a2b`; `eeee6b` and `ce832f` have
  subagent and side-model usage outside the assistant stream).
- The largest session's full history.
- Whether `updated_at` moves with usage: moot for these 21 (all archived), open for a live session.
