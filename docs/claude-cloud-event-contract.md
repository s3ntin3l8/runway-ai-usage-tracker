# Claude cloud-runner usage: the event contract

**Status: draft for approval. No implementation follows until this is approved.**

This is the specification the cloud-runner collector is built and accepted against (epic #669, track 1).
It is derived from real responses, not assumptions: every wire fact below is recorded in
[`evidence/acceptance/claude-cloud-source-shape-20261029.md`](evidence/acceptance/claude-cloud-source-shape-20261029.md)
and its companion JSON files. Where the data does not settle something, this document says so and gives
the default.

## 1. Scope

Sessions of Claude Code that ran on Anthropic's cloud runner (`environment_kind == "anthropic_cloud"`
in `GET /v1/code/sessions`). Local and bridged sessions are scraped locally and are not touched. Chat,
Cowork, the local-versus-cloud cross-check (#1008), a UI toggle (#1026) and a health surface (#1023) are
out of scope, as is side-model capture for local sessions (#1073).

## 2. Goal

A cloud-runner session produces `usage_events` that look like a local Claude Code session's: one event
per assistant message with its model, time, tools, subagent, project and branch, **and** totals per turn
that equal what the client itself reports, including calls the message stream omits. `usage_events`
stays authoritative; nothing derived is hand-patched.

## 3. Sources and what each is trusted for

| Source | Trusted for | Not trusted for |
|---|---|---|
| History `assistant` records (`payload.type`, `message.*`) | per-message time, model, tools, subagent, input and cache tokens incl. the 1h/5m write split (equal to local transcripts for all 143 compared messages) | output tokens (a lower bound), side calls |
| History `result` records (`modelUsage`, `usage`) | per-turn, per-model totals incl. real output, side calls and Haiku; `modelUsage` is the complete accounting | message-level detail; it restarts from zero when the runner process restarts |
| History `system` records | `cwd`, `branch` (project context) | usage |
| List row (`config.*`, `external_metadata.*`) | repo names, branches, `last_event_at` as the change marker, `status` | usage totals (the `usage` block is built differently in different sessions), `updated_at` |

`costUSD` is a list-price estimate (`costBasis: list`), not billing. It is ignored: Runway prices tokens
with its own table (`provider_pricing`), exactly as for local events.

## 4. Event model

Two kinds of `usage_events`, both `provider_id = "anthropic"`, `source_app = "claude_code"`,
`entrypoint = "cloud"`, `kind = "message"`, `cost_usd = None`, ingested through
`EventIngestor(...).ingest(..., sidecar_id="anthropic-cloud")` (`CLOUD_SIDECAR_ID`; a sidecar cannot claim
that id; dedup on `(provider_id, event_id)`).

- **Message event**: one per distinct assistant `message.id` (the last record of that id).
- **Remainder event**: one per closed turn and model, holding what `modelUsage` reports for that turn
  beyond the message events of the same turn and model.

A *turn* is the span of history records up to and including one `result` record (counted by
`sequence_num` order). Totals per turn and model therefore equal the `modelUsage` delta exactly, while
attribution (time, tools, subagent) is as fine as the stream allows.

### 4.1 Identity (idempotent replay)

`digest = sha256(cloud session id)[:32]`; the cloud id itself is never stored or logged.

- message event: `event_id = "cloud:{digest}:m:{message.id}"`
- remainder event: `event_id = "cloud:{digest}:r:{result_ordinal}:{model_id}"` where `result_ordinal` is
  the 1-based count of `result` records in sequence order.

History is append-only and ordered, so a re-read produces the same ids and the unique index absorbs it.

### 4.2 Field map

| `UsageEventPush` field | Message event | Remainder event |
|---|---|---|
| `event_id` | per 4.1 | per 4.1 |
| `ts` | `payload.timestamp`, else the record's `created_at` (median 0.3 s later, 0.2% over 60 s) | `created_at` of the `result` record |
| `model_id` | `normalize_anthropic_model(message.model)`; `<synthetic>` messages are skipped | model key of `modelUsage`, normalized the same way |
| `session_id` | `digest` | `digest` |
| `subagent_type` | `subagent_type` of the record when `parent_tool_use_id` is set (`subagent` if the record names none), else None | None |
| `turn_id` | None (like local Claude Code events) | `event_id` of the turn's last message event **of any model**, so the sessions view's `msgs` does not grow (a Haiku-only remainder included); the remainder's own `event_id` only if the turn has no message event at all |
| `tokens_input`, `tokens_cache_read` | `usage.input_tokens`, `usage.cache_read_input_tokens` | remainder, per 5.3 |
| `tokens_output` | `usage.output_tokens` (a lower bound) | remainder, per 5.3 (carries the real output) |
| `tokens_cache_create`, `_1h`, `_5m` | `usage.cache_creation_input_tokens` with `cache_creation.ephemeral_{1h,5m}_input_tokens` | remainder, split per 5.3 |
| `tokens_reasoning` | 0 | 0: `thinkingTokens` is a part of `outputTokens`, and the ingestor prices reasoning on top of output, so adding it would count it twice |
| `cwd` | the session's current `cwd` per 5.5 | same |
| `project_ref` | repository name per 5.5 | same |
| `git_branch` | session branch per 5.5 | same |
| `tool_names`, `tool_calls` | names of `tool_use` blocks, their count | None, 0 |
| `stop_reason`, `service_tier` | from the record when present | None |
| `app_version` | the latest `system/init` `claude_code_version` | same |
| `effort` | `config.effort_level` of the list row | same |
| `account_id` | the collector's canonical account | same |
| `raw_json` | None | None |

`project` is derived by the ingestor from `project_ref`/`cwd` as for local events.

Non-usage side output: the list row's `title` (string, non-empty after cleaning) is upserted as a
session name (`provider_id` `anthropic`, `session_id` = digest, `source_app` `claude_code`, kind
`generated`) on every list pass, change-only. `task_summary` / `post_turn_summary` are not names. It
creates no event and is never logged.

## 5. Rules

### 5.1 Finding and reading sessions

1. List all pages (`limit=20`, an omitted `next_cursor` on the last page ends the list; shared parser
   `page_with_terminal_omission`). Keep rows with `environment_kind == "anthropic_cloud"`.
2. A session needs reading when its `last_event_at` is later than the stored one (the list's
   `updated_at` is unrelated to usage and is not used). New sessions are read from the start.
3. Read `GET /v1/code/sessions/{id}/events` (`limit=50`, `sort_order=asc`, `cursor`). The API pages by
   an opaque cursor only, so the checkpoint stores the last **closed turn** boundary: its
   `committed_sequence` and the entry cursor of the page that holds it (encrypted, never logged).
   Resuming requests that cursor and skips records with `sequence_num <= committed_sequence`; a rejected
   cursor falls back to reading from the start with the same skip (log label `cursor_rejected`). The open
   tail after the last result is read again each cycle; message ids make re-emission harmless.
   `sequence_num` is a numeric string, starts at 1 and was contiguous with no duplicates in every
   session read; a gap, duplicate or reorder fails the session's cycle
   (`unsupported_contract/sequence`) and writes nothing for it.
4. A session is processed completely (its new turns) before its checkpoint advances. Failures isolate
   per session and per account.

### 5.2 Turn handling

- Records are buffered per open turn: assistant records by `message.id` (last record wins).
- On a `result` record the turn closes: emit its message events, then its remainder events, then
  advance the checkpoint (events first, checkpoint second; a crash replays harmlessly by 4.1).
- Records after the last `result` form an open tail. They are emitted, without remainders, once the
  session has had no new event for 1 hour (settle window), and marked `unsettled` in the cycle log. The
  list block cross-check is skipped for a session with an open tail.

### 5.3 Remainder computation

Per closed turn and model `m`:

1. `delta_m` = the turn's `modelUsage[m]` minus the previous result's `modelUsage[m]` (field by field:
   input, output, cache read, cache write, thinking). If any field of `m` decreased the process
   restarted, so `delta_m` is the new value itself (a restart is not an error).
2. `msgs_m` = the sums over the turn's message events of model `m`, field by field. A message id that
   was already emitted in an earlier turn is skipped (`repeated_message`): a resumed runner replays
   earlier messages, which were not new calls. The state keeps a short digest of every emitted id.
3. `remainder_m` = `max(delta_m - msgs_m, 0)` per field. Where that would be negative for some field
   and the main loop alone (the messages without a subagent marker) fits within `delta_m` in every
   field, the overshoot is the subagent messages': older runners leave subagent calls out of
   `modelUsage`, so those are real calls on top of it, and the comparison is repeated against the main
   loop only (`subagent_outside_model_usage`). Any other negative difference is clamped to zero and
   counted (`negative_remainder`); it never produces a negative event. Messages can thus exceed the
   delta (a subagent call outside `modelUsage`, or counters lost to a runner restart), and the events
   then carry that excess.
4. Cache-write TTL split of the remainder: proportional to the 1h/5m split of the session's message
   events so far; with no message events yet, all 1h (the observed norm). The split of a remainder is
   an estimate and is documented as such.
5. Emit a remainder event only if some field is positive (the ingestor would otherwise store an
   all-zero row and count it as a message).
6. The reducer validates what the ingestor does not: token counts are non-negative integers (anything
   else counts as 0); a timestamp that does not parse skips that message (its tokens are then covered by
   the remainder, which is computed from the events actually emitted) and is counted (`bad_timestamp`);
   a cache-write split that does not add up to the total is adjusted to it (`ttl_adjusted`).

Subagent messages count towards `msgs_m` of their model. A model that appears only in `modelUsage`
(Haiku) has `msgs_m = 0`, so its whole delta is a remainder event.

### 5.4 Normalization

Model ids go through `normalize_anthropic_model` (`app/services/model_normalization.py`) after a
context-window suffix is cut off: `modelUsage` keys a long-context run `claude-opus-4-8[1m]`, which the
shared normalizer would read as a different model (`opus-4`). An unrecognized id stays as reported and
shows in the existing unpriced-models check. Timestamps are
ISO 8601 UTC.

### 5.5 Project, branch, working directory

- `cwd`: the most recent `system` record's `cwd` before the message (init records carry it). It is
  only passed on when the event has a `project_ref` and a path segment of the `cwd` equals it: with
  none, the ingestor would label the project with the last path segment, and a bare `/home/user`
  would become the project `user`.
- `project_ref`: a repository **name** (the last path segment of
  `config.outcomes[].git_info.repo`, else `config.sources[].url`; never a URL or owner). With exactly
  one repository named, every event of the session carries that name, whatever its `cwd` (a session
  that ran mostly in a bare home directory still belongs to its repository); with several: the one
  whose name is a segment of the `cwd`; otherwise None.
- `git_branch`: `system.branch` when present, else the row's `external_metadata.current_branches` entry
  for the repository, else None.
- A single-repository session whose `cwd` is a bare home directory or an unrelated directory keeps
  its `project_ref` and has `cwd` None. A session with several repositories and no matching `cwd`
  stays unattributed: that is better than a wrong project.

### 5.6 Cross-check, never a gate

After a session settles (no open tail), compare the sum of its events with the list row's
`external_metadata.usage` (tokens and `cost_usd`) and log counts only: `agree`, `block_higher`,
`block_lower`, `no_block`. The block is built differently in different sessions (equal to the final
`modelUsage` in two, about twice it in one, the first run only in another, and absent on seven of 21
rows), so a disagreement is information, not an error, and never blocks, deletes or rewrites an event.

### 5.7 Budgets and pacing

One full sweep lists about 80 pages. Reading a session costs one request per 50 history records. Limits
per cycle: 40 history pages and 200 total requests; a paused backfill resumes at the stored turn
boundary (5.1), and while a cycle ends with work left the next one runs after 120 s instead of the
configured interval. Pacing 0.25 s between requests, the endpoint lock held per request, a busy gate skips the
cycle, as in the shipped collector. The first backfill of the largest known session (above 100 pages) is
spread over several cycles. Logs carry counts and fixed labels only: no ids, cursors, titles, cwd or
branch values.

## 6. State kept

One row per cloud session (keyed by `digest`, account): last `sequence_num`, last `last_event_at`,
previous result's `modelUsage` per model (for 5.3), result ordinal, message and remainder counters, last
cross-check result. No content, no ids. The coarse-collector tables are replaced.

## 7. Acceptance criteria (checked on prod, read-only SQL plus the cycle log)

1. **Totals per turn.** For every settled session, per model, the sum of its events' tokens is never
   below the final `modelUsage` (with restarts summed) and equals it field for field wherever the
   messages do not exceed it; an excess is explained by subagent calls outside `modelUsage` or a
   runner restart and is counted in the cycle log (`subagent_outside_model_usage`,
   `negative_remainder`).
2. **Messages.** Input, cache read and cache write of each message event equal the history record; the
   count of message events equals the count of distinct assistant `message.id` (excluding synthetic).
3. **Models.** Every event has a normalized `model_id`; no event carries a model it did not use; Haiku
   appears as remainder events.
4. **Time.** No event is dated at the sweep time, and no message event is dated after the result of
   its turn.
5. **Project.** Every event of a session with a single repository has that `project_ref`, whatever
   its `cwd` (and `cwd` NULL where no segment is the repository); sessions with none, or with several
   repositories and no matching `cwd`, have NULL, not a guess.
6. **Replay.** Running the collector again adds zero rows, and the cycle log reports zero new events.
7. **Scope.** No bridge sessions produce events; no event carries a cloud session id.
8. **Cost sanity.** For sessions with a block, Runway's priced cost is within a few percent of the
   block's `cost_usd` when they agree on tokens (a check, not a gate; differences come from list prices
   and the cache TTL split).
9. **Consumers.** The sessions view shows these sessions with project, branch, multiple models and a
   `msgs` count equal to the message events; top projects includes them.

## 8. Migration

A `app/services/maintenance/` plan/apply pair, offered as a Data Health fix, removes the events of the
first (coarse) collector (`entrypoint = "cloud"`, `event_id LIKE 'cloud:%'`, not `LIKE 'cloud:%:m:%'` and
not `LIKE 'cloud:%:r:%'`), then rebuilds the Anthropic rollups (on every apply, so an interrupted run is
finished by applying again) and the closed windows that overlapped the removed events (only in the run
that removed them; the script prints the range and can replay it). The first collector's two state
tables are left alone: they are unused once the replacement is in place, and clearing them under a
still-scheduled first collector would reset its baseline and bill every session again. Plan shows
counts, sums and the windows it would rebuild; apply is idempotent.

**Guard:** while events of the old form exist, the new collector writes nothing and reports the outcome
`migration_required`, so the order of deploy, migration and enabling cannot double count. The new
collector then backfills from history.

### 8.1 Relabelling stored events

Events are deduplicated on `(provider_id, event_id)`, so events stored before a labelling change (the
`anthropic-cloud` sidecar id, the single-repository project rule) keep their old labels. The
`cloud_events_reset` Data Health fix (`scripts/reset_cloud_events.py`) removes the history-based events
(`cloud:%:m:%` and `cloud:%:r:%`) **and** the per-session checkpoints (events alone would leave nothing
to re-emit; checkpoints alone would replay into the old rows), rebuilds the Anthropic rollups and the
closed windows that overlapped them, and drops the collector's in-memory backlog. The collector then
reads every session's history again. It never touches the first collector's events, so the migration
guard is unaffected; applying it twice is a no-op.

## 9. Known limits and open items

- `modelUsage` can be smaller than the messages of a turn (subagent calls it leaves out in older
  runners, counters lost when a runner restarts). The events then carry the messages' excess, so a
  session's total can exceed the client's own `modelUsage` figure. In the 21 sessions checked, 12 match
  it exactly and 9 carry such an excess, each with a counted cause.
- Remainder events are separate rows, and most rollups, top models, source apps and history count every
  row as one message, so for cloud sessions those global message counts run about 10-25% high (one
  extra row per turn and model). The sessions view and top sessions, hosts and projects collapse
  remainders through `turn_id` and stay exact. Accepted trade-off; token and cost totals are exact.
- `haiku-5.5` is priced at its own row with a long-prompt tier: an event whose prompt
  (`input + cache read + cache create` tokens) exceeds 100,000 tokens bills entirely at the higher rates
  ($0.50 in / $2.50 out), otherwise $0.10 / $0.50. Message events (`:m:`) carry a real per-call prompt
  size and use the tier. Remainder events (`:r:`) aggregate several calls and have no single prompt
  size, so they always price at the standard tier: for Haiku 5.5 a remainder's cost is a lower bound.
  Stored events priced before the tier existed need `scripts/recost_events.py --provider anthropic`.
- Per-message output is a lower bound; real output arrives in remainder events. Per-message output is
  therefore not attributable to a single message.
- Remainder events have no tools and no per-message time; they are dated at the result.
- The cache-write TTL split of a remainder is an estimate (5.3.4).
- The list block's inconsistencies (about 2x in one session, first run only in another) are unexplained;
  the contract does not depend on them.
- A cloud session later resumed locally could in principle appear in local transcripts too. None of
  about 290 cloud message ids occurs locally today; no de-duplication is built, the risk stays
  documented.
- The largest known session has not been read to its end (above 100 pages); its page count sets the
  backfill duration, not the design.
- Side-model usage in **local** sessions is tracked in #1073.
- Not verified against live data: behaviour for a session that is still running (the open tail rule is a
  precaution, as every captured session was archived).
