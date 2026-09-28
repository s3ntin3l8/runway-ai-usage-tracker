# Hermes Collector

Runway extracts session token usage and estimated/actual costs sidecar-side from local Hermes Agent SQLite databases across default and named profiles.

## Architecture

| Component | Where it runs | What it does |
|---|---|---|
| Event extractor (sidecar) | `scripts/sidecar_pkg/event_extractors/hermes.py` | Queries `state.db` instances, calculates incremental deltas, and maps canonical providers |
| Sidecar runner | `scripts/sidecar.py` | Discovers Hermes DB paths, builds canonical hints, and dispatches extraction cycles |
| Detection rules | `scripts/sidecar.py` `__REGISTRY__["hermes"]` | Discovers Hermes installations via `~/.hermes/state.db` and `~/.hermes/config.yaml` |
| Watermark state | `~/.config/runway/sidecar/hermes_watermark.json` | Persists per-slice high-water marks for ongoing sessions |

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

Because `session_model_usage` counters accumulate over the lifetime of a session slice (or long-running Discord thread), the extractor maintains high-water marks in `~/.config/runway/sidecar/hermes_watermark.json` (or `$RUNWAY_CONFIG_DIR/sidecar/hermes_watermark.json`).

On each sidecar poll:
- The extractor compares current tokens and costs against the recorded high-water mark for each slice.
- Only positive deltas (`delta_in`, `delta_out`, `delta_cost`, etc.) are emitted.
- Monotonic event IDs are generated:
  ```
  hermes|<profile>|<session_id>|<model>|<task>|<api_call_count>
  ```
- Stable event IDs ensure that discrete tasks and ongoing sessions never trigger duplicate-event drops or double-counting in Runway's `EventIngestor`.

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
   - Runway's `EventIngestor` detects `account_source == "default"` and **holds back** the event in the `pending_usage_events` table, excluding it from account totals and quota gauges.

2. **Operator Assignment via UI:**
   The operator sees the unassigned events in Runway's **Fleet View** -> **`PendingUsageEventsCard`** ("Unassigned usage · X events"), selects the desired account (e.g. `s3ntin3l8@gmail.com`), and clicks **Assign**.

3. **Persistent Tagging & Auto-Matching (`account_source="tag"`):**
   Assigning creates a `credential_tags` record (`provider:minimax -> s3ntin3l8@gmail.com`) scoped to this sidecar host. On future cycles:
   - `/fleet/config` delivers `server_account_tag_hints`.
   - `_build_canonical_hints_for_provider("hermes")` forwards the hint to `parse_hermes_events`.
   - Future events are automatically tagged with `account_id="s3ntin3l8@gmail.com"` and `account_source="tag"`, merging directly into the quota gauge.

4. **Native Hermes Events:**
   Unmapped non-canonical events retain the host's Hermes identity (from `HERMES_ACCOUNT_LABEL` or `"default"`) and leave `account_source=None` to defer to the sidecar's host attribution loop.

## Configuration

| Environment Variable | Default | Purpose |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Override root directory for Hermes state and configuration |
| `HERMES_ACCOUNT_LABEL` | `default` | Host-level account identifier used for native unmapped Hermes events |
| `RUNWAY_CONFIG_DIR` | `~/.config/runway` | Base directory for sidecar configuration and watermark storage |
| `SIDECAR_BOOTSTRAP_DAYS` | `90` | Number of days of historical sessions to bootstrap on first run |

## Troubleshooting

### No events collected
- Ensure `~/.hermes/state.db` or `~/.hermes/profiles/*/state.db` exists and has executed sessions.
- Check sidecar logs for `[hermes] collecting...` and ensure `hermes` is not excluded in `RUNWAY_PROVIDERS`.
- Check if `hermes_watermark.json` already recorded the sessions. To re-scan, delete `~/.config/runway/sidecar/hermes_watermark.json`.

### Events appear as "Unassigned usage" in Fleet UI
- This is normal for newly routed canonical providers (e.g. MiniMax, Kimi).
- Navigate to the Fleet dashboard, open the **Unassigned usage** card, and assign the events to your configured account. Future events will map automatically.
