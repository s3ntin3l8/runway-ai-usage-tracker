# Collection Logic

## Overview

The collection system uses a bucket-based approach: strategies are categorized by the type of data they provide, collected in phases, and merged into a single card per provider.

For providers that emit per-message logs (Claude, Codex, Gemini, OpenCode), the
sidecar additionally extracts events into `usage_events` and pushes them via
`/api/v1/fleet/ingest`. The card produced by the strategy pipeline carries the
authoritative quota gauge (`pct_used`, `limit_value`, `reset_at`); per-model and
per-sidecar splits are derived on demand from `usage_events` by the
`/api/v1/usage/fleet` endpoint's `window_aggregations` field. See the
[event-sourced data model spec](superpowers/specs/2026-05-08-event-sourced-usage-data-model.md)
for the full event flow.

## Strategy Types

| Type | Description |
|------|-------------|
| **quota** | Provides usage/limit data: percentages, currency limits, tier info |
| **mixed** | Provides both quota and enrichment data in one response |
| **enrichment** | Provides token breakdown, session counts, model usage details |

## Per-Collector Strategy Mapping

| Collector | Strategy | Type |
|-----------|----------|------|
| **Anthropic** | oauth | quota |
| Anthropic | web | quota |
| Anthropic | cli | mixed |
| Anthropic | statusline | mixed |
| Anthropic | local | enrichment |
| **ChatGPT** | web | quota |
| ChatGPT | cli | mixed |
| ChatGPT | local | enrichment |
| **Gemini** | api | quota |
| Gemini | local | enrichment |
| **OpenCode** | api | quota |
| OpenCode | sidecar | quota |
| OpenCode | web | mixed |
| OpenCode | local | enrichment |

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