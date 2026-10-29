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

## Accounting: what this capture can and cannot say

The capture kept only the **first five** `result` records of each session (median session: 5 of 13;
largest: 5 of 111; the smallest session has 2 and both were kept). Any `modelUsage` figure below for
the median session is therefore **result 5 of 13, not the final total**, and no conclusion about the
`modelUsage` total or about which source matches billing can be drawn from it.

| Session | Source | Input | Output | Cache read | Cache write |
|---|---|---:|---:|---:|---:|
| median | list block (`cost_usd` 8.5278) | 4,850 | 62,876 | 25,790,004 | 285,294 |
| median | assistant messages, last record per `message.id` (138 messages) | 294 | 2,883 | 23,526,585 | 277,736 |
| median | `result.modelUsage` of result 5 of 13 (not the final one) | 1,249 | 59,794 | 18,244,329 | 267,604 |
| smallest | list block (only `cost_usd` 3.3098; no token counters) | n/a | n/a | n/a | n/a |
| smallest | assistant messages (61 messages) | 412 | 570 | 4,906,696 | 243,527 |
| smallest | `result.modelUsage` of the last (2nd) result | 918 | 34,688 | 5,006,052 | 273,114 |
| largest | list block (`cost_usd` 752.70) | 184,208 | 5,487,271 | 2,173,590,630 | 37,427,708 |

The largest session's history was cut at 15,000 records (300 pages), so it is not comparable.

What the data does support:
1. **Assistant `output_tokens` are streaming placeholders** (2,883 against about 60,000 in
   `modelUsage`), so per-message output cannot be summed. Input and cache counts per message are real.
2. `modelUsage` values are monotonically increasing across the results of one session, per model.
3. One `cost_usd` is not enough to reconstruct billing: with Runway's rates the median session's block
   tokens price to 7.86 USD (all cache writes at the 5-minute rate) or 8.72 USD (all at the 1-hour
   rate) against 8.53 USD reported, and the rates, the write-TTL share and the model split are all
   unknown. This needs a capture that keeps every result record and its `costUSD`.

## Not established yet

- Every `result` record's `modelUsage` (token fields and `costUSD`), so the final per-model totals and
  the effective rates cannot yet be compared with the block and its `cost_usd`.
- The largest session's full history (about 5,800+ assistant records seen in the first 15,000).
- Whether the sources differ at all once the final `modelUsage` is used, and if they do, whether the
  difference is subagent usage (`task_progress` / `task_notification` `usage.total_tokens`).
- Whether `updated_at` moves with usage: moot for these 21 (all archived), open for a live session.
