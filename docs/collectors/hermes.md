# Hermes Collector

**File:** `scripts/sidecar_pkg/event_extractors/hermes.py` (sidecar only — not a server provider)

Runway extracts session token usage and estimated/actual costs sidecar-side from local Hermes Agent SQLite databases across default and named profiles.

> **Hermes is not a server provider.** It has no entry in
> `app/core/registry.json` — it exists only as a **sidecar overlay provider** in
> `scripts/sidecar_registry_overlay.json` (`add_providers.hermes`), whose
> reason reads *"the server must not list it as a provider"*. The server never
> collects a Hermes quota card; it only receives the usage events the sidecar
> extracts. This is the same arrangement `minimax_code` has — see
> [`minimax.md`](minimax.md) — and it is why Hermes is absent from the
> provider list in the dashboard.

## Architecture

| Component | Where it runs | What it does |
|---|---|---|
| Event extractor (sidecar) | `scripts/sidecar_pkg/event_extractors/hermes.py` | Queries `state.db` instances, calculates incremental deltas, and maps canonical providers |
| Sidecar runner | `scripts/sidecar.py` | Discovers Hermes DB paths, builds canonical hints, and dispatches extraction cycles |
| Detection rules | `scripts/sidecar.py` `__REGISTRY__["hermes"]` | Discovers Hermes installations via `~/.hermes/state.db` and `~/.hermes/config.yaml` |
| Watermark state | `~/.config/runway/sidecar/hermes_watermark.jsonl` | Persists per-slice high-water marks for ongoing sessions |

## Overview

Hermes AI Agent runs autonomous tasks (e.g. PR reviews, discord bots, cron jobs) and routes LLM requests across diverse backends (MiniMax, Kimi, OpenCode, OpenRouter, DeepSeek, xAI, etc.). Hermes stores its execution state and token metrics in SQLite databases operating in WAL mode.

### Database Discovery

The extractor automatically discovers Hermes databases across:

1. **Environment override:** `$HERMES_HOME/state.db`
2. **Default profile:** `~/.hermes/state.db`
3. **Named profiles:** `~/.hermes/profiles/*/state.db` (e.g. `~/.hermes/profiles/review-bot/state.db`)

Databases are opened using URI read-only mode (`file:<path>?mode=ro`) to prevent SQLite lock contention with active Hermes agent writers.

### Storage & Schema

Hermes records metrics in two primary tables:

1. **`sessions`**: Session metadata including `id`, `source` (`api_server`, `discord`, `cron`, `webui`), `profile_name`, `started_at`, `cwd`, `git_branch`, and totals.
2. **`session_model_usage`**: Granular breakdown of model and task slices:
   - Primary key: `(session_id, model, billing_provider, billing_base_url, billing_mode, task)`
   - Counters: `api_call_count`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`
   - Costs: `estimated_cost_usd`, `actual_cost_usd`, `cost_status`, `cost_source`
   - Timestamps: `first_seen`, `last_seen`

### Incremental Delta Tracking

Because `session_model_usage` counters accumulate over the lifetime of a session slice (or long-running Discord thread), the extractor maintains high-water marks in `~/.config/runway/sidecar/hermes_watermark.jsonl` (or `$RUNWAY_CONFIG_DIR/sidecar/hermes_watermark.jsonl`).

On **Windows** the same file lives under `%APPDATA%\runway\sidecar\`
(`hermes_watermark.json`), falling back to `~/AppData/Roaming/runway/sidecar/`
when `APPDATA` is unset.

On each sidecar poll:
- The extractor compares current tokens and costs against the recorded high-water mark for each slice.
- Only positive deltas (`delta_in`, `delta_out`, `delta_cost`, etc.) are emitted.
- Monotonic event IDs are generated:
  ```
  hermes|<profile>|<session_id>|<model>|<task>|c<api_call_count>s<emission_seq>
  ```
- Stable event IDs ensure that discrete tasks, ongoing sessions, and late cost updates never trigger duplicate-event drops or double-counting in Runway's `EventIngestor`.
- **Watermark Lifecycle & Bounding:** High-water marks are keyed by `(resolved_db_path, account, profile, session, model, billing_provider, task)`. The database path is stored once per database rather than once per slice, so it is no longer the bulk of the file. Each slice records a `last_seen` timestamp, which is what lets the compaction below reclaim the slices whose source is gone — a database that no longer exists, or a session that Hermes deleted from a database that is still there.
- **Entrypoint Propagation:** Sessions preserve their runtime origin in Runway's `entrypoint` field: default API sessions set `entrypoint="hermes"`, while sessions originating from specific platforms set `entrypoint="hermes-<source>"` (e.g. `hermes-discord`, `hermes-cron`, `hermes-webui`).

### The Watermark Log

`hermes_watermark.jsonl` is an **append-only log**: one JSON line per change, each holding the database it belongs to and the slices that changed.

```json
{"db": "/Users/you/.hermes/state.db", "accounts": {"default": {"default|<session>|<model>|…": {…}}}}
```

A cycle appends the slices that moved instead of rewriting the whole file. That is what the file was rewritten for: the whole state was serialized and written on every cycle that changed anything, and the Hermes extraction runs on every poll, so the bytes written per day grew with a file that never stopped growing.

- **The write is flushed and synced before the cycle returns.** The emission counter of a slice lives in the watermark *and* in the event id, so a lost update means an event id the server has already seen — and the usage behind it would be dropped.
- **A forgotten slice is appended as `null`**, so the log says it is gone and the next cycle does not put it back.
- **A damaged line is skipped, not fatal.** A crash during the write can leave a torn line; the log is a cache, and refusing to read it would lose every mark in it.
- **The log is compacted** once it holds more lines than the state it carries: the state is then the cheaper file to write, and the compaction writes every mark out, so it cannot lose one.

The database path is stored **once per database** — it used to be repeated in the key of every slice, which is where most of the bytes of a large watermark went. The account is a level of the state, so an account id that holds a `|` cannot shift the fields behind it.

An existing install's flat `hermes_watermark.json` is read **once**, when the log does not exist yet, and written out in the new format. Every mark that can be read is carried over — a mark is the difference between what the sidecar already reported and what it would report next, so a lost mark is a session whose whole lifetime is reported a second time. The legacy file is **left on disk**: a sidecar that is reverted reads it and re-reports only the usage since the migration, where a missing file would make it re-report the whole Hermes history.

### Watermark Compaction

The watermark used to only ever grow: a deleted profile, a test fixture that was unpacked and removed, or a folder the user cleaned up left its slices behind forever — and the whole file was rewritten on every cycle that changed anything, so the bytes written per day grew with a file that never stopped growing.

A slice's high-water mark is now forgotten when **all** of these hold:

- **its `last_seen` is older than 30 days** — the session has been quiet long enough that its row cannot advance unless a writer reappears, and
- **its source is provably dead**, which is one of
  - **its database file no longer exists**, or
  - **its row is gone from a database that is still there** — the session was deleted from Hermes, so the row behind the slice is gone with it.

The idle window and the proof of death are both load-bearing, because the server cannot catch a forgotten mark on its own:

- The server deduplicates usage events on the **exact** `(provider_id, event_id)` and keeps the first row it stored. A re-emitted Hermes event carries a restarted counter (`c<calls>s<emission_seq>`), so its id differs from the original and the server files it as *new* usage.
- Without a baseline the extractor would report the session's **whole lifetime** as new usage instead of the increment — the server would count that session twice.

So the mark of everything that could still move is kept: an active session (recent `last_seen`), a database that exists but was not discovered, and a database whose rows could not be listed (a permission error is not "it is gone"). The one residual case is a session that sat idle for more than 30 days, whose source was deleted, and which comes back and resumes activity — it is counted once extra.

The compaction runs even when the host has **no Hermes database left at all** — that is exactly the host whose watermark is full of dead keys, so finding nothing to collect is not a reason to skip it.

A live database's slices are reclaimed one row at a time: the extractor lists the slices that are still in `session_model_usage` (whose primary key *is* the slice identity) and forgets the mark of the slices whose row is gone. A live session's row can advance, so it is never pruned on age alone.

That reclaim only fires when Hermes deletes the `session_model_usage` row along with the session — `session_model_usage.session_id` references `sessions(id)` `ON DELETE CASCADE`, and SQLite only enforces that with `PRAGMA foreign_keys=ON`. If Hermes leaves orphan usage rows behind, the rule reclaims nothing: the rows are still there, so their slices are still alive.

## Canonical Provider Mapping

Hermes routes requests to multiple upstream LLM providers. The extractor maps Hermes `billing_provider` values onto canonical Runway provider IDs:

| Hermes `billing_provider` | Canonical Runway `provider_id` |
|---|---|
| `kimi-coding` | `kimi_coding` |
| `minimax`, `minimax-oauth`, `minimax-coding-plan` | `minimax` |
| `opencode-go`, `opencode-zen`, `opencode` | `opencode` |
| `openrouter` | `openrouter` |
| `deepseek`, `deepseek-api` | `deepseek` |
| `anthropic` | `anthropic` |
| `gemini` | `gemini` |
| `ollama`, `ollama-cloud` | `ollama` |
| `xai` | `xai` |
| Unmapped custom providers | `hermes-<billing_provider>` |

## Multi-Account Attribution & Pending Events

### Why Hermes has no upstream account identity

Neither `state.db` nor Hermes configuration files store upstream account identities or emails:
- `sessions.user_id` stores chat platform user IDs (e.g. Discord snowflake IDs), not LLM provider accounts.
- `session_model_usage` only stores the backend name (`billing_provider`).
- `auth.json` contains API keys or OAuth tokens whose JWT payloads omit email claims (e.g. `openai-codex` and `xai-oauth` contain only subject identifiers).

Consequently, the Hermes sidecar cannot prove which operator account or email owns a message billed through an underlying provider such as MiniMax or Kimi.

### The Holding-Back Contract

To prevent unmapped events from corrupting labeled quota gauges or leaking across accounts:

1. **Unhinted Canonical Events (`account_source="default"`):**
   When an event maps to a canonical provider (e.g. `minimax`) and no tag hint exists yet on the sidecar:
   - The extractor stamps `account_id="default"` and `account_source="default"`.
   - Host-level Hermes identities (such as `HERMES_ACCOUNT_LABEL="bot-team"`) are withheld from canonical events so they do not create phantom accounts under upstream providers.
   - Runway's `EventIngestor` detects `account_source == "default"` and **holds back** the event in the `pending_usage_events` table, excluding it from account totals and quota gauges — unless the host's own native identity resolves it (see *Resolving from the host's native identity* below).

2. **Resolving from the host's native identity:**
   A Hermes event is billed to the account of the upstream it called, and the sidecar only ever learns the provider. The **host's native collector** for that provider does know: it reported the credential, and Runway filed it under the account the credential resolved to. That `credential_sources` row is the machine's own answer, and `EventIngestor` uses it to stamp the event (`account_source="local"`, stored as `attribution_source="local"`) instead of holding it back.

   Three rules keep this from becoming a cross-host attribution leak:

   - **Per host.** Only the accounts *this* sidecar reported for the provider are considered. A deployment-wide identity never reaches a host that did not report it (the same rule as #319).
   - **The credential the host presents today.** A source answers only when it is the newest row for its origin, still enabled, and not rejected by the provider. A rotation — the same file now holding a different login — leaves the previous row behind, and that row is history: the newer one answers instead. A source the operator turned off, or one the provider refused (`auth_failed`), identifies nothing. A host whose current credential has no resolved identity yet (still in the pending slot) has no answer, so its events are held back rather than stamped on the login it used to have. Exactly one account answers: zero (nothing to resolve from) or several (two current credentials, two accounts) leave the event held for the operator, exactly as before.
   - **Never over a tag.** An operator tag always wins over the machine's answer, and an archived account takes no new usage — a retired account holds the event back rather than receiving it.

   **Coverage.** The rule can only fire for a provider whose native credential carries an identity. On the shipped sidecar that is **Anthropic** (the Claude Code OAuth account email) and **xAI** (the Grok CLI identity); providers whose credential carries no email — MiniMax, Kimi, Ollama, OpenRouter, DeepSeek — have nothing to resolve from, so their events are still held for assignment until the operator tags them once. The rule is not a universal fix: it removes the manual tag for the providers that can answer, and leaves the rest exactly where they were.

   **Related providers are not consulted.** The lookup asks for `push.provider_id` only. A host whose Google identity lives on its `antigravity` credential therefore does not resolve its `gemini` events — `related_account_provider_ids()` maps `gemini` → `antigravity`, but that mapping stays an explicit operator tag.

   **Already-held events.** An event that was held back resolves on a later push once the provider can be answered, and its pending copy is dropped with it — the "Unassigned usage" card stops listing an event that is already counted.

3. **Operator Assignment via UI:**
   The operator sees the unassigned events in Runway's **Fleet View** -> **`PendingUsageEventsCard`** ("Unassigned usage · X events"), selects the desired account (e.g. `s3ntin3l8@gmail.com`), and clicks **Assign**.

4. **Persistent Tagging & Auto-Matching (`account_source="tag"`):**
   Assigning creates a `credential_tags` record (`provider:minimax -> s3ntin3l8@gmail.com`) scoped to this sidecar host. On future cycles:
   - `/fleet/config` delivers `server_account_tag_hints`.
   - `_build_canonical_hints_for_provider("hermes")` forwards the hint to `parse_hermes_events`.
   - Future events are automatically tagged with `account_id="s3ntin3l8@gmail.com"` and `account_source="tag"`, merging directly into the quota gauge.

   **Archived accounts and related providers.** The account list also offers:
   - *Archived accounts* of the same provider. Usage is stored on the archived account, so it is hidden from Fleet but still counted in Stats and archived lifetime stats. Disabled-but-not-archived accounts are never offered.
   - *Accounts of a related provider*: Gemini usage can be assigned to an Antigravity account (Google's subscription replaced gemini-cli). The events are re-homed to `provider_id="antigravity"` and the tag stores `target_provider_id`, so later Gemini events from that host are redirected server-side at ingest (no sidecar change). The allow-list is `_RELATED_ACCOUNT_PROVIDERS` in `app/services/account_identity.py`.

   Both mappings are per host. A sidecar with a stale hint cache can keep stamping events with the previous account for up to ~10 minutes. Hermes sends raw Gemini model ids (e.g. `gemini-3-pro-preview`) that don't match the Antigravity price rows, so redirected events fall back to Hermes' reported cost or show as unpriced.

5. **Native Hermes Events:**
   Unmapped non-canonical events retain the host's Hermes identity (from `HERMES_ACCOUNT_LABEL` or `"default"`) and leave `account_source=None` to defer to the sidecar's host attribution loop.

## Configuration

| Environment Variable | Default | Purpose |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Override root directory for Hermes state and configuration |
| `HERMES_ACCOUNT_LABEL` | `default` | Host-level account identifier used for native unmapped Hermes events (intentionally not used for canonical retargeting, where events are held back as `default` / `PendingUsageEvent`) |
| `RUNWAY_CONFIG_DIR` | `~/.config/runway` | Base directory for sidecar configuration and watermark storage |
| `SIDECAR_BOOTSTRAP_DAYS` | `90` | Number of days of historical sessions to bootstrap on first run |

## Troubleshooting

### No events collected
- Ensure `~/.hermes/state.db` or `~/.hermes/profiles/*/state.db` exists and has executed sessions.
- Check sidecar logs for `[hermes] collecting...` and ensure `hermes` is not excluded in `RUNWAY_PROVIDERS`.
- Check if `hermes_watermark.jsonl` already recorded the sessions. To re-scan, delete `~/.config/runway/sidecar/hermes_watermark.jsonl`.

### Events appear as "Unassigned usage" in Fleet UI
- This is normal for newly routed canonical providers (e.g. MiniMax, Kimi).
- Navigate to the Fleet dashboard, open the **Unassigned usage** card, and assign the events to your configured account. Future events will map automatically.
- A provider whose native credential carries an account identity on this host (Anthropic, xAI) resolves without the card — see *Resolving from the host's native identity*. If the events are still unassigned, that host has no such credential, or more than one account for that provider.

### Where the resolution lives
- `app/services/credential_sources.py` — `sidecar_native_accounts()` reads the accounts this host reported for a provider.
- `app/services/event_ingestor.py` — `EventIngestor._native_identity_account()` applies the per-host, unambiguous, never-over-a-tag rules.

## Related Files

| File | Purpose |
|---|---|
| `scripts/sidecar_pkg/event_extractors/hermes.py` | The Hermes event extractor |
| `scripts/sidecar_pkg/canonical_providers.py` | Maps Hermes provider names onto Runway's canonical ids |
| `scripts/sidecar_pkg/event_watermark.py` | Per-store read watermark |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | The extractor it is modelled on |
| `scripts/reclassify_hermes_pending.py` | One-shot reclassification of pending Hermes events |
