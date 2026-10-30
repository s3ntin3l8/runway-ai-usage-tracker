# Collection Logic

## Overview

The collection system uses a bucket-based approach: strategies are categorized by the type of data they provide, collected in phases, and merged into a single card per provider.

For providers that emit per-message logs (Claude, Codex, Gemini, OpenCode), the
sidecar additionally extracts events into `usage_events` and pushes them via
`/api/v1/fleet/ingest`. The card produced by the strategy pipeline carries the
authoritative quota gauge (`pct_used`, `limit_value`, `reset_at`); per-model and
per-sidecar splits are derived on demand from `usage_events` by the
`/api/v1/usage/fleet` endpoint's `window_aggregations` field. See the
[Data Model section of architecture.md](architecture.md#data-model) for the full event flow.

## Current-card reconciliation

`LatestUsage` is the live card view. Each producer reports complete provider
snapshots independently: server collectors reconcile after a fresh complete
response, and sidecars include `completed_providers` with each ingest cycle.
Cards omitted from a complete snapshot are removed from that producer's live
contribution; snapshots from cached, skipped, failed, or partial collection
cycles do not remove anything. Failures mark retained last-good cards stale
after an hour. The source contribution table lets one producer reconcile
without removing another producer's card for the same provider/account.
Server-side accounts that disappear from the active collector set are treated
like skipped collections: their last-good contributions are retained and aged
to stale after the same one-hour grace period, rather than being mistaken for
a successful empty snapshot.
When multiple sources report the same slot, a fresh server contribution keeps
the materialized `sidecar_id` as `local`; otherwise the freshest healthy
sidecar owns the slot. If every contribution is stale, the most recently
updated source owns it.

`BaseCollector.complete_snapshot(result)` defaults to `False`. Each registered
collector explicitly opts in with `COMPLETE_SNAPSHOT = True` only when it emits
full provider snapshots; collectors with result-dependent completeness should
override the method. Valid empty results must also use
`successful_empty_result` so only a provider-confirmed empty snapshot can retire
old cards.
Reconciliation only changes the live card view; `quota_snapshots` and
`usage_events` retain their historical data.

## Strategy Types

There are exactly **two** buckets, and a strategy is in one of them or the
other. The bucket is not declared as a type — it is derived from the strategy's
options: a strategy whose options carry `{"enrich": True}` is an **enrichment**
strategy, and everything else is a **primary** strategy.

| Bucket | How it is declared | What it does |
|---|---|---|
| **primary** | anything without `{"enrich": True}` | Produces the card. The **first** primary strategy that succeeds wins; the rest are not run. |
| **enrichment** | `{"enrich": True}` in the strategy's options | Runs **after** a primary strategy has succeeded, and merges extra fields into the primary card. It never acts as a fallback for missing quota data. |

There is no third bucket. A strategy cannot return quota and enrichment
separately in one response — there is no `mixed` strategy type, and no code
path anywhere in `app/services/collectors/` that would produce one.

The only options key any collector may pass is `enrich`. See
`app/services/collectors/base.py:138-140`.

> **No shipped collector declares an enrichment strategy.** The mechanism is
> implemented in the base class, but every collector in the registry uses only
> primary strategies. The per-message token breakdowns and session counts that
> reach the dashboard arrive as **sidecar usage events**, not as a server-side
> enrichment strategy.

Enrichment events also carry **project context** — the working directory (`cwd`), `git_branch`, and the tool names invoked in the message (`tool_names`). The server derives `project` (the basename of `cwd`) in `EventIngestor` and indexes it, which is what powers the Sessions project column and the Top Projects / Top Tools rankings. The derivation is **host-independent**: a client's working directory is a path on *that* machine (`D:\Projects\repo` on Windows, `/home/u/repo` on macOS/Linux), and the server runs on Linux, so the label is the last path segment of either separator form rather than whatever the server's `os.path` would split on.

A sidecar additionally resolves the **git repository** the working directory belongs to and reports its **name** (`project_ref`) — the remote URL never leaves the client, because it can carry credentials. The server prefers that name over the folder's name, so two checkouts of one repository group together even when their folders are named differently. A client that cannot resolve one (no `git`, not a repository) simply reports none, and the folder's name is the label.

**Which label a session gets.** A session run in a git repository, or in a worktree of one, is labelled with the **repository name** (`project_ref`). A session not run in a repository is labelled with the **folder it was run in** (the last segment of its `cwd`, e.g. `Projects`). The precedence in `EventIngestor` is `project_ref` first, the `cwd`-derived label second. For a worktree whose repository can no longer be resolved (most are deleted after merging), the `cwd` fallback cuts the path at the first worktree-container directory — `.claude`, `.wt`, `.worktrees`, `.worktree` or `.mullion-worktrees` (`WORKTREE_CONTAINERS` in `app/services/project_label.py`) — so `<repo>/.wt/<slug>/app` is labelled `<repo>`. The segment must be a whole one, preceded by a separator, in either separator form and any case. A bare `worktrees` folder (no dot) is deliberately not a container. The operator can add more names under Settings > System > Project labels (`worktree_patterns`, up to 20 entries of at most 64 characters; one directory name, no path separators, `*` the only wildcard, matched against whole segments, case-insensitively). They are added to the built-ins, never replace them, and `normalize_cwd` is the only place that applies them. **One cut rule, scanning the segments left to right and acting on the first worktree directory:** a *container* (every built-in, and any pattern that does not start with `*`, e.g. `worktrees-extra`, `wt-*`) is cut off with everything after it, so `<repo>/worktrees-extra/<slug>/app` is labelled `<repo>`. A *sibling* pattern (starts with `*`, then a literal suffix, e.g. `*-wt`) names a folder kept beside the repository: the path is cut after that segment and the suffix is stripped from it, so `/home/u/repo-wt/<slug>/app` is labelled `repo`; a segment that is nothing but the suffix is cut like a container. The patterns are read through a cached getter (`app/services/worktree_patterns.py`, invalidated when Settings saves them), and the `worktree_project_labels` Data Health check uses the same list, so saving a pattern and applying the check relabels history. Rows stored before this rule are relabelled by the `worktree_project_labels` Data Health check, which prefers a session's reported `project_ref` over the `cwd`.

They also carry **the client app that made the call** (`source_app`: `opencode`, `claude_code`, `minimax_code`, ...). That is not the same as the provider the request was billed to — several client apps share one provider, and a canonical-provider retag folds them onto one id. `source_app` survives the retag, so "which app spent the quota" stays answerable (`/usage/source-apps`).

Rows written **before** the column existed are backfilled with `provider_id`, which is exactly what the ingestor falls back to when an older sidecar reports no app. Those rows therefore show the provider id (`minimax`, `opencode`, ...) under the app label until enough new events arrive to separate them — the split is only as precise as the data behind it, not a bug in the chart.

## Per-Collector Strategy Mapping

The "Runs in" column shows where the strategy executes. The server-side collectors only do `api` / `web`; everything that needs filesystem, CLI, or LSP access lives in the sidecar and reaches the server through `/api/v1/fleet/ingest`.

| Collector | Strategy | Bucket | Runs in |
|-----------|----------|--------|---------|
| **Anthropic** | oauth | primary | server |
| Anthropic | web | primary | server |
| **Anthropic Console** | web | primary | server |
| **Antigravity** | api | primary | server |
| **ChatGPT** | web | primary | server |
| **DeepSeek** | — | primary | server |
| **Gemini** | api | primary | server |
| **GitHub** | — | primary | server |
| **Kimi API** | — | primary | server |
| **Kimi Coding** | api, web | primary | server |
| **Kimi K2** | — | primary | server |
| **MiniMax** | — | primary | server |
| **Ollama** | api, web | primary | server |
| **OpenCode** | api, web | primary | server |
| **OpenRouter** | — | primary | server |
| **xAI** | api | primary | server |
| **zAI** | — | primary | server |

A dash means the collector does not declare a `STRATEGIES` dict; it implements
the abstract `_primary_strategy` / `_fallback_strategies` path instead. The
order within a `STRATEGIES` dict is the **declaration order**, and that is the
order the primary strategies are tried in — first success wins.

The sidecar is not a strategy and does not appear in this table. It is a
separate producer: it reads local sources (transcripts, CLI caches, browser
cookie stores) and pushes **usage events** to `/api/v1/fleet/ingest`. It never
runs inside a collector and never competes with a server strategy for the same
card.

The two MiniMax strategies need different credentials: the platform API secret
(`sk-…`) and the MiniMax Code app's OAuth token (`mmoat_…`). Each host accepts
only its own — sending the wrong kind comes back as **HTTP 200** with
`base_resp.status_code` 1004 "login fail", so the failure is only visible in the
body. The collector reports that as an **error card**, not as an empty plan:
this collector reports complete snapshots, so a healthy-looking "No active plan"
card would retire the user's real session and weekly rows. Which kind a
credential is comes from the `mmoat_` prefix, so a token pasted into Settings
or the database is still routed to the host that accepts it.

The app's auth store keeps one record per build and region, each with its own
expiry. The registry reaches it with a `records.*` wildcard, and both the
server and the sidecar pick the record whose expiry is still in the future — a
dead record must not win just because its key sorts first, and the token and
its expiry must come from the *same* record.

### MiniMax Code pricing: no free/paid signal

MiniMax Code traffic is priced the way every other subscription plan is: the
events carry no cost, and the server prices them against the pay-as-you-go
API rates (see the MiniMax block in `app/services/pricing_seed.py`). The
result is a **notional** figure — what the usage would have cost on the API —
not an amount that was billed.

That distinction matters because MiniMax Code's runtime ledger carries **no
free/paid flag at all**: its usage rows have `cost_usd = 0` (the M Plan is a
subscription, so the ledger's own cost column is always zero), their model
column is `NULL`, and the only model string in the ledger is the session's
`effectiveModel` (`minimax/MiniMax-M3.1-Flash`). There is no model catalog,
pricing table, quota table or billing table beside them. So "is this model
free?" is not a question the source can answer — the answer is missing
upstream, not lost in Runway.

Two consequences worth keeping in mind:

- **A model that is covered by a plan is not a free model.** MiniMax lists
  MiniMax-M3.1-Flash at $0.15 / $0.60 / $0.015 per M tokens (input / output /
  cache read); under the M Plan the included quota draws down at that price.
  Runway reporting a non-zero cost for it is the API-equivalent convention
  above, not a pricing error.
- **A real free model would price at $0 automatically.** The cost calculator
  treats a `-free` / `:free` / `/free` model id as free and skips the pricing
  fallback, and OpenCode's free catalog is filed under its own `opencode-free`
  provider for the same reason. If the MiniMax Code runtime ever starts
  emitting a free id, the existing guard prices it at $0 with no Runway
  change — the MiniMax extractor only strips the `minimax/` prefix, so such a
  suffix survives normalization.

## Collection Pipeline

`BaseCollector.collect()` has **two** phases, not four.

```
┌──────────────────────────────────────────────────────────────┐
│                    collect(provider)                          │
├──────────────────────────────────────────────────────────────┤
│  Phase 1: PRIMARY                                            │
│  ├── Partition the strategies into primary and enrichment   │
│  │   (by the presence of {"enrich": True} in their options) │
│  ├── Run the primary strategies in declaration order        │
│  ├── The FIRST one that does not return an error card wins  │
│  ├── If every primary strategy errored, the best error      │
│  │   card wins (highest priority error_type), else the      │
│  │   collector's own error handler runs                     │
│  └── Capture the primary result's metadata so enrichment can │
│      align its window boundaries (reset_at vs fixed cutoffs) │
├──────────────────────────────────────────────────────────────┤
│  Phase 2: ENRICHMENT                                         │
│  ├── Run the enrichment strategies, in declaration order     │
│  ├── Merge each successful enrichment into the primary card  │
│  │   via _enrich_results()                                   │
│  └── An enrichment strategy that fails is skipped — it never │
│      discards the primary result                            │
└──────────────────────────────────────────────────────────────┘
```

Enrichment only runs when a primary strategy has already succeeded. It is
never a fallback: if the primary strategies all failed, the enrichment
strategies are not run at all.

### Rate limiting

There is no cross-source priority ladder and no generic "fall back to the web
strategy when the API is rate limited" rule. Rate limiting is handled
**per collector**, in `SmartCollector`:

- a `429` marks the provider as rate-limited and the backoff state is
  recorded;
- while that backoff is active, the collector is skipped rather than
  retried.

A strategy failing for any other reason is simply "the next primary strategy
is tried".

## Conflict Resolution

### Primary data

The first primary strategy that does not return an error card wins. There is
no comparison between strategies, no quality ranking, and no cross-source
priority. "Sidecar" is not a strategy and never competes with a server
strategy — the sidecar's contribution is reconciled separately, by
`accumulator.py`, and the freshest healthy sidecar owns a slot when no fresh
server contribution exists.

### Enrichment data

`_enrich_results()` matches an enrichment dict to a primary card by
`(service_name, variant, window_type, model_id)`, with graceful fallback to
less specific keys — a card is also looked up by `(variant, window_type,
model_id)`, then `(window_type, model_id)`, then `(model_id)`. An enrichment
dict with `model_id=None` acts as an aggregate fallback for any primary card.

When a match is found, the enrichment **overwrites** these three fields on the
card:

| Field | What happens |
|---|---|
| `token_usage` | replaced by the enrichment value |
| `by_model` | replaced by the enrichment value |
| `msgs` | replaced by the enrichment value |

Nothing is maximised and nothing is summed. `pct_used` is then re-derived from
the primary card's own `used_value` / `limit_value`, so the enrichment can
never change the quota percentage — only the token and message breakdown.

## Claude cloud-runner sessions

Local scraping already covers CLI, Desktop and bridge sessions. Claude Code sessions that run
on Anthropic's cloud runner leave no local transcript, so a server-side collector
(`app/services/claude_cloud_collector.py`, `CLAUDE_CLOUD_COLLECTOR_ENABLED`, default off) reads
their history. The rules are specified in
[the cloud event contract](claude-cloud-event-contract.md); the wire facts behind them are in
[the source-shape evidence](evidence/acceptance/claude-cloud-source-shape-20261029.md).

- **Which sessions.** `GET /v1/code/sessions` is listed page by page (the real API omits
  `next_cursor` on the last page, which the collector, like the verifier, treats as the end; a
  declared `has_more` that disagrees with the cursor fails the cycle as `unsupported_contract`). Only
  rows whose top-level `environment_kind` is `anthropic_cloud` count; `bridge` rows are local
  sessions mirrored to claude.ai and are skipped. A session is read when its `last_event_at` moved
  (the row's `updated_at` is unrelated to usage and is not used).
- **What is written.** The session's history (`/v1/code/sessions/{id}/events`) goes through
  `claude_cloud_events`: one **message event** per assistant message (model, time, tools, subagent,
  input and cache tokens with the 1h/5m write split, project and branch) and, when a `result` record
  closes a turn, one **remainder event** per model for what its cumulative `modelUsage` reports beyond
  those messages (side calls, the real output, a second model such as Haiku). Events carry
  `sidecar_id` `server`, `entrypoint` `cloud`, and a digest as `session_id`; their ids are derived
  from the history, so a replay is a duplicate, never a second event. The API's `costUSD` and the
  list row's usage block are not used for pricing; Runway prices the tokens itself.
- **Resuming.** Each session has a checkpoint (`claude_cloud_session_checkpoint`): the sequence
  number of its last closed turn and the encrypted cursor of the page that holds it, plus the
  reducer's state. The next read starts there; the open tail after the last result is read again.
  A cursor the API rejects falls back to reading from the start. Events are committed before the
  checkpoint moves.
- **Open tail.** Messages after the last result are emitted once the session has been quiet for an
  hour (without a remainder).
- **Cross-check.** After a session settles, its events are compared with the list row's usage block
  and only a label is logged (`agree`, `block_higher`, `block_lower`, `mixed`, `no_block`); the block
  is built differently in different sessions, so it never gates or rewrites anything.
- **Budgets.** Per cycle at most 40 history pages and, always, 200 requests; a session that could not close a
  turn within its pages gets a larger page budget next time (never beyond the request cap; a turn longer
  than that is skipped until the session changes again and reported as `stalled_turn_skipped`), and while work is left the next cycle runs
  after 120 s instead of the interval, reusing the unfinished sessions instead of listing again. The 120 s pace also holds when a cycle cannot see its login (`source_ineligible`) while sessions are still pending, but only for `MAX_UNREAD_CATCH_UPS` (3) such cycles in a row that read nothing; after that the interval applies until a cycle reads again, and the collector logs `unread_catch_up_capped` once when it falls back. Logs carry counts and fixed labels only.
- **Migration.** While events of the first (coarse) collector exist (`cloud:<digest>:<n>` ids) the
  cycle writes nothing and reports `migration_required`; the `cloud_coarse_events` Data Health fix
  removes them.
- Known limits: message counts in rollups, top models and history run high for cloud sessions
  (remainders are separate rows; the sessions view and top sessions/projects collapse them); the
  cache-write TTL split of a remainder is an estimate; per-message output is a lower bound. A cloud
  session later resumed on a local machine could also appear in local transcripts; none of about 290
  checked message ids does, and there is no cross-source de-duplication.

## Implementation Notes

- The strategy options dict carries exactly one key: `enrich`. There is no
  `type` field.
- `BaseCollector.collect()` (`app/services/collectors/base.py:283`) partitions
  the strategies, runs the primary ones until one succeeds, then runs the
  enrichment ones.
- `_enrich_results()` (`app/services/collectors/base.py:401`) does the merge.
  There is no `_merge_enrichment()` method anywhere in the codebase.
- A strategy returns a **list** of card dicts, or a list containing a single
  error card. There is no `{"quota": ..., "enrichment": ...}` shape.

## Expiry units

A credential's expiry may be published in seconds, milliseconds, microseconds or
nanoseconds. The reader does **not** take the unit from the rule — it reads it off the
magnitude of the number, because the registry maps an expiry field by name
(`expiry_date`) without saying which unit that field carries.

Both sides of the fence implement the same rule, and
`tests/unit/test_mapping_resolver_parity.py` pins the two copies together. They have to
agree: the server decides whether a credential is fresh and the sidecar reports
freshness independently, so a divergence means the two sides disagree about whether a
credential is alive.

| | copy | read by |
|---|---|---|
| server | `app/core/expiry.py::expiry_seconds` | `app/services/credential_provider.py` (the freshness probe) and `app/core/utils.py` (`IdentityExtractor.exp_from_tokens`, the single source of truth for a *stored* bundle's expiry — the token cache, the auto-refresher and Token Health all read through it) |
| sidecar | `scripts/sidecar_pkg/expiry.py::expiry_seconds` | `scripts/sidecar.py` (the freshness probe) and `scripts/sidecar_pkg/xai_renewer.py` (a JWT's `exp` claim) |

The rule, as both copies implement it:

| The raw value is | Read as | Divide by |
| --- | --- | --- |
| `>= 1e17` | nanoseconds | 1e9 |
| `>= 1e14` | microseconds | 1e6 |
| `>= 1e11` | milliseconds | 1e3 |
| anything smaller | seconds | — |

A 2026 epoch is ~1.8e9 s, ~1.8e12 ms, ~1.8e15 us and ~1.8e18 ns, so each threshold sits
clear of the next. A millisecond value has to be ~3000 years in the future before it
would be read as microseconds, and it then lands in the past — expired, which is the safe
direction. The old reader divided by 1000 exactly once, so a microsecond epoch landed
thousands of years ahead and read as **fresh forever**; because the record picker takes
the *largest* expiry, a dead microsecond record also outranked a live millisecond one.

A value that is not a number, and a value that is not a plausible expiry at all (past the
last second of year 9999), both read as **no expiry claim** — never as fresh. So do `nan`
and an infinity: `nan` compares False against everything, so without an explicit check it
would pass every test and be reported as an expiry of its own. An unknown expiry must not
win the largest-expiry picker over a record that states one.

Every expiry field the registry maps today is epoch milliseconds
(`claudeAiOauth.expiresAt`, `expiresAt`, `records.*.expiresAtMs`, `expiry_date`), so this
is a robustness change rather than a fix for a live report. A provider that publishes
another unit needs no rule change: the magnitude table covers s/ms/us/ns.
