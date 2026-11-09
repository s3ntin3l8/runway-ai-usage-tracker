# Collection Logic

How a provider's response becomes a card, how cards from several producers are
reconciled, and how per-message events get their project and session labels.

## Overview

Two producers feed the dashboard:

- **Server collectors** call provider APIs and web endpoints on a poll
  interval and produce **cards** (quota gauges).
- **Sidecars** read what only a workstation can see — logs, CLI caches, browser
  cookies, keychains — and push **usage events** (and, where needed, cards) to
  `POST /api/v1/fleet/ingest`.

A card carries the authoritative quota gauge (`pct_used`, `limit_value`,
`reset_at`). Per-model and per-sidecar splits are derived on demand from
`usage_events`. See the [data model](architecture.md#data-model) for the full
event flow.

## Card provenance labels

Every card carries two labels, rendered raw on the credential rows as
`data_source / input_source` (for example `api / config`).

**`data_source`: how the data was obtained**

| Value | Meaning |
|---|---|
| `api` | An official API or OAuth endpoint. The most reliable source. |
| `web` | A web endpoint called with a session cookie or scraped. More detail, but it breaks when the site changes. |
| `local` | A local log, CLI output or application database. **Never emitted by a server collector** — a `local` card reached the server because a sidecar pushed it. |
| `error` | The card reports a failure instead of a quota. |
| `lsp` | A legacy value. No shipped collector emits it, but ingest still recognises it as the one exception to the rule that a sidecar may not push percentage-quota cards: quota cards come from server API collectors, and a sidecar quota card with any other `data_source` is dropped at ingest. |

**`input_source`: where the credential came from**

| Value | Meaning |
|---|---|
| `config` | Entered in Settings and stored in the database. |
| `server` | Found by the server process itself: an environment variable, `.env`, or Runway's own `github_oauth.json`. |
| `sidecar` | Forwarded by a sidecar (host logs, browser cookies, CLI logins, keychains). |

## Current-card reconciliation

`LatestUsage` is the live card view. Each producer reports complete provider
snapshots independently: server collectors reconcile after a fresh complete
response, and sidecars include `completed_providers` with each ingest cycle.
Cards omitted from a complete snapshot are removed from that producer's live
contribution; snapshots from cached, skipped, failed or partial cycles remove
nothing. A failure marks retained last-good cards stale after an hour. A
per-source contribution table lets one producer reconcile without removing
another producer's card for the same provider and account. Server-side accounts
that disappear from the active collector set are treated like skipped
collections: their last-good contributions age to stale after the same grace
period instead of being mistaken for an empty snapshot.

When several sources report the same slot, a fresh server contribution wins and
the slot's `sidecar_id` is materialised as `local`; otherwise the freshest
healthy sidecar owns it. If every contribution is stale, the most recently
updated source owns it.

`BaseCollector.complete_snapshot(result)` defaults to `False`. A collector opts
in with `COMPLETE_SNAPSHOT = True` only when it emits full provider snapshots;
one whose completeness depends on the result overrides the method. A valid empty
result must use `successful_empty_result`, so only a provider-confirmed empty
snapshot can retire old cards. Reconciliation changes only the live view;
`quota_snapshots` and `usage_events` keep their history.

## Strategies and the collection pipeline

A collector declares an ordered list of **strategies**. There are two buckets,
derived from each strategy's options:

| Bucket | Declared as | Behaviour |
|---|---|---|
| **primary** | anything without `{"enrich": True}` | Produces the card. The **first** primary strategy that does not return an error card wins; the rest are not run. |
| **enrichment** | `{"enrich": True}` | Runs after a primary strategy succeeded and merges token and message breakdowns into its card. Never a fallback for missing quota data. |

There is no global ranking of `api` over `web` over `local`; the declaration
order within one collector is the priority. A strategy returns a list of card
dicts, or a list holding one error card.

`BaseCollector.collect()` runs two phases:

1. **Primary.** Run the primary strategies in order; the first success wins. If
   all errored, the highest-priority error card is returned (or the collector's
   own error handler runs).
2. **Enrichment.** Only after a primary success, run the enrichment strategies
   and merge each successful result via `_enrich_results()`. A failing
   enrichment is skipped; it never discards the primary result.

`_enrich_results()` matches an enrichment to a primary card by `(service_name,
variant, window_type, model_id)`, falling back to less specific keys (an
enrichment with `model_id=None` acts as an aggregate fallback). On a match it
**overwrites** `token_usage`, `by_model` and `msgs`, then re-derives `pct_used`
from the primary card's own `used_value` / `limit_value`, so enrichment can
never change the quota percentage.

> No shipped collector declares an enrichment strategy. The mechanism exists in
> the base class, but per-message breakdowns reach the dashboard as **sidecar
> usage events**.

**Rate limiting** is per collector, in `SmartCollector`: a `429` puts the
provider into a backoff state and the collector is skipped until it ends. There
is no generic "fall through to the web strategy on 429" rule.

The sidecar is not a strategy. It is a separate producer and never competes with
a server strategy for the same card; reconciliation above decides which
contribution a slot shows.

### Per-collector strategies

All server strategies run in the server and only do `api` / `web`.

| Collector | Strategies |
|---|---|
| Anthropic | oauth, web |
| Anthropic Console | web |
| Antigravity, Gemini, xAI | api |
| ChatGPT | web |
| Kimi Coding, Ollama, OpenCode | api, web |
| DeepSeek, GitHub, Kimi API, Kimi K2, MiniMax, OpenRouter, zAI | implement the abstract primary / fallback path instead of a `STRATEGIES` dict |

### MiniMax credentials

The MiniMax collector accepts two kinds of credential: the platform API secret
(`sk-…`) and the MiniMax Code app's OAuth token (`mmoat_…`). Each host accepts
only its own, and sending the wrong kind comes back as **HTTP 200** with
`base_resp.status_code` 1004 "login fail", so the failure is visible only in the
body. The collector reports it as an **error card**, not an empty plan: it
reports complete snapshots, so a healthy-looking "No active plan" card would
retire the user's real rows. The kind is read from the `mmoat_` prefix, so a
token pasted into Settings is still routed to the host that accepts it.

The app's auth store keeps one record per build and region, each with its own
expiry. Server and sidecar both pick the record whose expiry is still in the
future, and the token and expiry come from the *same* record.

### MiniMax Code pricing: no free/paid signal

MiniMax Code events carry no cost, so the server prices them against the
pay-as-you-go API rates (the MiniMax block in `app/services/pricing_seed.py`).
The result is a **notional** figure — what the usage would have cost on the API
— not an amount billed. The app's runtime ledger has no free/paid flag: its cost
column is always 0, usage rows have no model, and no catalog, pricing or billing
table sits beside them. So "is this model free?" cannot be answered from the
source.

- A model covered by a plan is not a free model. A non-zero cost for
  MiniMax-M3.1-Flash is the API-equivalent convention, not a pricing error.
- A real free model prices at $0 automatically: the cost calculator treats a
  `-free` / `:free` / `/free` model id as free, and the extractor only strips the
  `minimax/` prefix, so such a suffix survives.

## Project labels

Events carry a working directory (`cwd`), `git_branch` and tool names. The
server derives a **project label** in `EventIngestor` and indexes it, which
powers the Sessions project column and the Top Projects / Top Tools rankings.
Events also carry the **client app** that made the call (`source_app`:
`opencode`, `claude_code`, `minimax_code`, …), which is not the provider the
request was billed to — several apps share one provider. `source_app` survives
a canonical-provider retag, so "which app spent the quota" stays answerable.
Rows written before the column existed show the provider id under the app label
until newer events separate them. OpenCode events also carry the gateway tag as
`raw_provider_id` when the sidecar forwards it.

**Which label a session gets**, in order:

1. **`project_ref`** — the git repository name the sidecar resolved for the
   `cwd`. The remote URL never leaves the client, because it can carry
   credentials. Two checkouts of one repository group together.
2. **The `cwd` fallback** — the last path segment, in either separator form
   (a client path like `D:\Projects\repo` is labelled correctly on a Linux
   server). For a worktree whose repository can no longer be resolved, the path
   is cut at the first worktree directory, so `<repo>/.wt/<slug>/app` is
   labelled `<repo>`.

**Worktree directories.** The built-in containers are `.claude`, `.wt`,
`.worktrees`, `.worktree` and `.mullion-worktrees` (`WORKTREE_CONTAINERS` in
`app/services/project_label.py`); a bare `worktrees` folder is deliberately not
one. Operators can add more under Settings → System → Project labels
(`worktree_patterns`: up to 20 entries of at most 64 characters, one directory
name, `*` the only wildcard, matched against whole segments, case-insensitive).
Patterns add to the built-ins, never replace them. The path is scanned left to
right and acted on at the first worktree directory:

- a **container** pattern (every built-in, and any pattern not starting with
  `*`, like `wt-*`) is cut off with everything after it;
- a **sibling** pattern (starts with `*` and a literal suffix, like `*-wt`)
  names a folder kept beside the repository: the path is cut after that segment
  and the suffix stripped, so `/home/u/repo-wt/<slug>/app` is labelled `repo`.

**A session that moved into a worktree.** A session is labelled from its
shallowest `cwd`. If it has no `project_ref` and one of its `cwd`s runs through a
worktree directory, the label comes from that worktree — but only when the
repository it holds lies inside the session's shallowest directory, which is
what distinguishes a parent folder from a different repository the session
merely touched.

Rows stored under an older rule are relabelled by the `worktree_project_labels`
Data Health check, which prefers a session's `project_ref` over its `cwd`.

### Which events get a `project_ref`

The sidecar resolves the repository of each event's `cwd` once per cycle
(`scripts/sidecar_pkg/git_identity.py`) with `git rev-parse --show-toplevel` and
`remote.origin.url`. Answers are kept in the sidecar's `git-identity.json`
(successes only, bounded, oldest dropped first), so a directory resolved once
keeps its name after the worktree is deleted or the sidecar restarts.

Events that cannot get one:

- **Deleted worktrees and clones first seen after deletion** (backfilled history).
  Nothing is guessed from the path, because a parent can belong to another
  repository.
- **Directories outside any repository.** Not remembered, so a directory that
  becomes a repository later is picked up.
- **Antigravity generated worktrees** (`<agy home>/.system_generated/worktrees/<id>`),
  which carry no name and are removed by agy.
- **Another host's paths.** A sidecar resolves only its own filesystem.
- **Events without a working directory**: Claude cowork sessions. The cloud
  collector sets its own `project_ref`.
- **No `git` on the sidecar's PATH**, or a lookup that fails or exceeds 3 s.
  These are not persisted, so the next cycle retries.

## Session names

A session name is a title a tool shows for a session. Sidecars send them in
`IngestRequest.session_names` (`{provider_id, session_id, source_app, name,
kind}`, `kind` is `agent` or `generated`) and the server stores them in
`session_names`. A name is free text and is never logged.

A sidecar extractor registers a `collect(out)` function in
`scripts/sidecar.py::_SESSION_NAME_COLLECTORS` under its provider id; it runs
once per cycle after that provider's events and must use the same `provider_id`
as those events. `run_collection` returns only names that changed since the last
accepted push (at most 2000 per push, the server's cap), and they ride the first
batch. The sent hashes are saved only once that batch was accepted
(`names_upserted` present, no `names_error`), so a failed or queued push sends
them again. The offline queue file holds names in clear text, like it holds
working directories; the state file holds only hashes.

| Tool | Provider / app | Kind | Source |
|---|---|---|---|
| Claude Code | `anthropic` / `claude_code` | `agent` and `generated` | `agent-name` and `ai-title` records in `~/.claude/projects` transcripts. Cowork transcripts are not covered. |
| opencode | per event provider / `opencode` | `generated` | `session.title` (`session_v2.title` after migration) |
| MiniMax Code | `minimax` / `minimax_code` | `generated` | `local_runtime_sessions.title`; child background sessions are skipped |
| Codex | `chatgpt` / `codex` | `agent` | `threads.name` in `state_*.sqlite`, else `session_index.jsonl` |
| Antigravity (agy) | `antigravity` / `antigravity` | `generated` | `title` in `conversation_summaries.db` |

Conversation content is never read for names: Codex's `title`, `preview` and
`first_user_message`, agy's `preview`, and subagent nicknames are all ignored.

## Claude cloud-runner sessions

Claude Code sessions that run on Anthropic's cloud runner leave no local
transcript, so a server-side collector
(`app/services/claude_cloud_collector.py`) reads their history. The rules are
specified in [the cloud event contract](claude-cloud-event-contract.md); the wire
facts behind them are in
[the source-shape evidence](evidence/acceptance/claude-cloud-source-shape-20261029.md).

- **Enabling.** On by default; switch it off in Settings → Providers → Claude or
  with `CLAUDE_CLOUD_COLLECTOR_ENABLED=false`. Turning it off stops new
  collection; written events stay. It reads cloud-runner sessions only
  (`environment_kind == anthropic_cloud`), never local files, and needs one
  eligible Claude Code OAuth login already held by the server (read-only, no
  refresh). With no login a cycle does nothing and logs nothing; with an
  ineligible one it logs `outcome=source_ineligible` and makes no request. It
  first runs about two minutes after start, then every
  `CLAUDE_CLOUD_COLLECTOR_INTERVAL_SECONDS` (default 1800), sooner while work is
  left.
- **Which sessions.** `GET /v1/code/sessions` is listed page by page; the API
  omits `next_cursor` on the last page, which counts as the end, and a declared
  `has_more` that disagrees with the cursor fails the cycle as
  `unsupported_contract`. Only `anthropic_cloud` rows count — `bridge` rows are
  local sessions mirrored to claude.ai. A session is re-read when its
  `last_event_at` moved.
- **What is written.** The session history goes through `claude_cloud_events`:
  one **message event** per assistant message (model, time, tools, subagent,
  input and cache tokens with the 1h/5m write split, project, branch) and, when a
  `result` record closes a turn, one **remainder event** per model for what its
  cumulative `modelUsage` reports beyond those messages. Events carry
  `sidecar_id` `server`, `entrypoint` `cloud` and a digest as `session_id`; ids
  derive from the history, so a replay is a duplicate, never a second event. The
  API's `costUSD` is not used: Runway prices the tokens itself.
- **Session names.** The list row's `title` is stored as the session's name
  (kind `generated`) on every list pass, including sessions not re-read. A failed
  names write is noted on the cycle and does not fail the usage sync.
- **Resuming.** Each session has a checkpoint with its last closed turn, the
  encrypted cursor of the page holding it, and the reducer state. A cursor the
  API rejects falls back to reading from the start. Events are committed before
  the checkpoint moves. Messages after the last result are emitted once the
  session has been quiet for an hour, without a remainder.
- **Budgets.** At most 40 history pages and 200 requests per cycle. A session
  that could not close a turn gets a larger page budget next time; a turn longer
  than the request cap is skipped (`stalled_turn_skipped`) until the session
  changes. While work is left the next cycle runs after 120 s; that pace is
  capped at `MAX_UNREAD_CATCH_UPS` (3) consecutive cycles that read nothing.
- **Health.** Each cycle upserts one `claude_cloud_collector_state` row per
  account. `GET /api/v1/system/status` reports it as `claude_cloud`, and Settings
  flags an account unhealthy after 3 failed cycles in a row or no cycle for 3
  intervals.
- **Migration.** While events of the first (coarse) collector exist
  (`cloud:<digest>:<n>` ids) the cycle writes nothing and reports
  `migration_required`; the `cloud_coarse_events` Data Health fix removes them.
- **Known limits.** Message counts in rollups, top models and history run high
  for cloud sessions (remainders are separate rows; the sessions view and top
  sessions and projects collapse them). A remainder's cache-write TTL split is an
  estimate and per-message output is a lower bound. There is no cross-source
  de-duplication against local transcripts.

### Bridge-session spot check

`scripts/check_bridge_sessions.py` compares the usage block of `bridge` list rows
with the sums of the same session's local events (`app/services/claude_cloud_bridge_check.py`).
It is a counts-only check that writes no events. By default it reads rows from a
JSON file (`--rows`); `--live --yes-call-provider` fetches them itself, read-only,
and exits 3 with `source_unavailable` without an eligible login. Input, cache
read and cache write are compared (an unknown value is never a mismatch); output
is excluded because the cloud's assistant `output_tokens` are placeholders. A
small positive input difference is expected — the side model's input is missing
from local transcripts — while cache counters should match exactly. Most bridge
rows carry no usage block, so this is a spot check, not coverage.

## Expiry units

A credential's expiry may be published in seconds, milliseconds, microseconds or
nanoseconds. The reader takes the unit from the **magnitude** of the number, not
from the rule, because the registry maps an expiry field by name
(`expiry_date`) without saying which unit it carries.

| The raw value is | Read as | Divide by |
|---|---|---|
| `>= 1e17` | nanoseconds | 1e9 |
| `>= 1e14` | microseconds | 1e6 |
| `>= 1e11` | milliseconds | 1e3 |
| anything smaller | seconds | — |

A 2026 epoch is about 1.8e9 s, 1.8e12 ms, 1.8e15 µs or 1.8e18 ns, so each
threshold sits clear of the next. A value that is not a number, not a plausible
expiry (past the last second of year 9999), `nan` or an infinity reads as **no
expiry claim** — never as fresh — so an unknown expiry cannot beat a record that
states one in the largest-expiry picker.

Both sides implement the same rule, and
`tests/unit/test_mapping_resolver_parity.py` pins the two copies together: the
server decides whether a credential is fresh and the sidecar reports freshness
independently, so a divergence would have them disagree about whether a login is
alive.

| | Copy | Read by |
|---|---|---|
| server | `app/core/expiry.py::expiry_seconds` | `app/services/credential_provider.py` (freshness probe) and `app/core/utils.py` (`IdentityExtractor.exp_from_tokens`, the single source of truth for a stored bundle's expiry — the token cache, the auto-refresher and Credentials all read through it) |
| sidecar | `scripts/sidecar_pkg/expiry.py::expiry_seconds` | `scripts/sidecar.py` (freshness probe) and `scripts/sidecar_pkg/xai_renewer.py` (a JWT's `exp` claim) |
