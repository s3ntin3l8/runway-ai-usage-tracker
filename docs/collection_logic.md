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

| Type | Description |
|------|-------------|
| **quota** | Provides usage/limit data: percentages, currency limits, tier info |
| **mixed** | Provides both quota and enrichment data in one response |
| **enrichment** | Provides token breakdown, session counts, model usage details |

Enrichment events also carry **project context** — the working directory (`cwd`), `git_branch`, and the tool names invoked in the message (`tool_names`). The server derives `project` (the basename of `cwd`) in `EventIngestor` and indexes it, which is what powers the Sessions project column and the Top Projects / Top Tools rankings. The derivation is **host-independent**: a client's working directory is a path on *that* machine (`D:\Projects\repo` on Windows, `/home/u/repo` on macOS/Linux), and the server runs on Linux, so the label is the last path segment of either separator form rather than whatever the server's `os.path` would split on.


They also carry **the client app that made the call** (`source_app`: `opencode`, `claude_code`, `minimax_code`, ...). That is not the same as the provider the request was billed to — several client apps share one provider, and a canonical-provider retag folds them onto one id. `source_app` survives the retag, so "which app spent the quota" stays answerable (`/usage/source-apps`).

Rows written **before** the column existed are backfilled with `provider_id`, which is exactly what the ingestor falls back to when an older sidecar reports no app. Those rows therefore show the provider id (`minimax`, `opencode`, ...) under the app label until enough new events arrive to separate them — the split is only as precise as the data behind it, not a bug in the chart.

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
| **OpenCode** | api / web | quota | server |
| OpenCode | local (SQLite events) | enrichment | sidecar |
| **Antigravity** | api (Code Assist cloud) | quota | server |
| Antigravity | local (LSP probe — legacy enrichment) | enrichment | sidecar |
| **MiniMax** | api (coding plan) | quota | server |
| MiniMax | api (open platform — MiniMax Code app login) | quota | server |
| MiniMax | local (SQLite runtime ledger — MiniMax Code) | enrichment | sidecar |

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
