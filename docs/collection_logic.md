# Collection Logic

## Overview

The collection system uses a bucket-based approach: strategies are categorized by the type of data they provide, collected in phases, and merged into a single card per provider.

For providers that emit per-message logs (Claude, Codex, Gemini, OpenCode), the
sidecar additionally extracts events into `usage_events` and pushes them via
`/api/v1/fleet/ingest`. The card produced by the strategy pipeline carries the
authoritative quota gauge (`pct_used`, `limit_value`, `reset_at`); per-model and
per-sidecar splits are derived on demand from `usage_events` by the
`/api/v1/usage/fleet` endpoint's `window_aggregations` field. See the
[Data Model section of CLAUDE.md](../CLAUDE.md#data-model) for the full event flow.

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

| Type | Description |
|------|-------------|
| **quota** | Provides usage/limit data: percentages, currency limits, tier info |
| **mixed** | Provides both quota and enrichment data in one response |
| **enrichment** | Provides token breakdown, session counts, model usage details |

Enrichment events also carry **project context** — the working directory (`cwd`), `git_branch`, and the tool names invoked in the message (`tool_names`). The server derives `project` (the basename of `cwd`) in `EventIngestor` and indexes it, which is what powers the Sessions project column and the Top Projects / Top Tools rankings.

## Per-Collector Strategy Mapping

The "Runs in" column shows where the strategy executes. The server-side collectors only do `api` / `web`; everything that needs filesystem, CLI, or LSP access lives in the sidecar and reaches the server through `/api/v1/fleet/ingest`.

| Collector | Strategy | Type | Runs in |
|-----------|----------|------|---------|
| **Anthropic** | oauth | quota | server |
| Anthropic | web | quota | server |
| Anthropic | cli | mixed | sidecar |
| Anthropic | statusline | mixed | sidecar |
| Anthropic | local (logs) | enrichment | sidecar |
| **ChatGPT** | web | quota | server |
| ChatGPT | cli | mixed | sidecar |
| ChatGPT | local (logs) | enrichment | sidecar |
| **Gemini** | api | quota | server |
| Gemini | local (session logs) | enrichment | sidecar |
| **OpenCode** | web | quota | server |
| OpenCode | local (SQLite DB) | enrichment | sidecar |
| **Antigravity** | api (Code Assist cloud) | quota | server |
| Antigravity | local (LSP probe — legacy enrichment) | enrichment | sidecar |

## Collection Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│                     collect(provider)                       │
├─────────────────────────────────────────────────────────────┤
│  Phase 1: QUOTA                                            │
│  ├── Run all quota strategies in priority order            │
│  │   Priority: api → sidecar → web                         │
│  └── Take first successful (or best available if         │
│      rate limited)                                         │
├─────────────────────────────────────────────────────────────┤
│  Phase 2: MIXED EXTRACTION                                 │
│  ├── Run all mixed strategies (cli, statusline, etc.)     │
│  └── Extract quota portion + enrichment portion           │
│      separately                                           │
├─────────────────────────────────────────────────────────────┤
│  Phase 3: ENRICHMENT                                       │
│  ├── Run all enrichment strategies (local)                │
│  └── Merge any enrichment extracted from mixed strategies │
├─────────────────────────────────────────────────────────────┤
│  Phase 4: MERGE                                            │
│  └── Combine: quota (best source) + enrichment (all)      │
│      → Single card per provider                            │
└─────────────────────────────────────────────────────────────┘
```

## Conflict Resolution

### Quota Data
- **Priority**: API > sidecar > web (unless rate limited)
- If API fails due to rate limiting, fall back to web
- Sidecar is treated as high-priority (remote but authoritative)

### Enrichment Data
- Token counts: take maximum value from all sources
- Session counts: take maximum
- Model usage: merge dictionaries, sum values for same models

### Mixed Strategies
- Must return `{"quota": LimitCard, "enrichment": dict}`
- Quota portion enters Phase 1 pool
- Enrichment portion enters Phase 3 pool

## Implementation Notes

- STRATEGIES dict gets `type` field in options dict
- BaseCollector `collect()` method refactored to use phases
- Mixed strategies refactored to split output
- `_merge_enrichment()` method handles conflict resolution
