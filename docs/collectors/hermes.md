# Hermes

**File:** `scripts/sidecar_pkg/event_extractors/hermes.py`

Sidecar-only extractor that turns the token usage and cost recorded in local Hermes Agent
SQLite databases (default and named profiles) into usage events. There is no server collector
and no quota card.

## At a glance

- **Sidecar only.** `hermes` is not in `app/core/registry.json`. It exists only as a sidecar
  overlay provider (`add_providers.hermes` in `scripts/sidecar_registry_overlay.json`), so the
  server never lists it. The server receives events and files them under the canonical
  provider they map to. See [minimax_code.md](minimax_code.md) for the same arrangement.
- **Events:** one event per positive delta of a `session_model_usage` slice
  (`kind="message"`). `entrypoint` is `hermes` for `api_server` sessions and
  `hermes-<source>` otherwise (`hermes-discord`, `hermes-cron`, `hermes-webui`).
- **No cards**, so `data_source`/`input_source` do not apply. Attribution is described under
  [Account attribution](#account-attribution).

## Credentials

None. The extractor reads local files; detection rules (`~/.hermes/state.db`,
`~/.hermes/config.yaml`) only tell the sidecar Hermes is installed.

| Source | Where | Runs in | Notes |
|---|---|---|---|
| `$HERMES_HOME/state.db` and `$HERMES_HOME/profiles/*/state.db` | env override | sidecar | Optional |
| `~/.hermes/state.db` | default profile | sidecar | |
| `~/.hermes/profiles/*/state.db` | named profiles | sidecar | |

Paths are resolved and deduplicated. Databases are opened read-only
(`file:<path>?mode=ro`) so active Hermes writers are never blocked.

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Extra Hermes root to scan |
| `HERMES_ACCOUNT_LABEL` | `default` | Host-level account for events that stay on Hermes (not for events mapped to an upstream provider) |
| `RUNWAY_CONFIG_DIR` | `~/.config/runway` | Base for sidecar state, including the watermark log |
| `SIDECAR_BOOTSTRAP_DAYS` | `90` | History window imported on first run |

## Usage events (sidecar)

Source tables in `state.db`: `sessions` (id, source, profile, start time, cwd, git branch) and
`session_model_usage`, keyed by `(session_id, model, billing_provider, billing_base_url,
billing_mode, task)` with call, token (input, output, cache read and write, reasoning) and
cost (`estimated_cost_usd`, `actual_cost_usd`) counters.

The counters accumulate over a session slice's life, so the extractor keeps a high-water mark
per slice and emits only positive deltas. Event id:

```
hermes|<profile>|<session_id>|<model>|<task>|c<api_call_count>s<emission_seq>
```

### Watermark log

State lives in `hermes_watermark.jsonl` under the sidecar state dir
(`~/.config/runway/sidecar/`, or `%APPDATA%\runway\sidecar\` on Windows). Code is in
`scripts/sidecar_pkg/hermes_watermark.py`.

- Append-only: each line holds a database path and the slices that changed. Marks are keyed by
  database, account, profile, session, model, billing provider and task, and each carries a
  `last_seen` time.
- Writes are flushed and synced before the cycle returns. The emission counter is part of the
  event id, so a lost mark would reuse an id the server already holds and drop usage.
- A torn line is skipped. The log is compacted once it holds more lines than the state.
- A flat legacy `hermes_watermark.json` is read once and recorded by a
  `hermes_watermark.json.migrated` marker; the legacy file is left on disk.

### Pruning

A mark is forgotten only when its slice has been idle for more than 30 days and its source is
provably gone (the database file is missing, or the row is gone from a database that still
exists). Both conditions matter: a re-emitted event with a restarted counter has a different id,
so the server would count the session's whole lifetime again. Pruning also runs on a host with
no Hermes database left.

## Account attribution

Hermes stores no upstream account identity: `sessions.user_id` is a chat-platform id and
`session_model_usage` only names `billing_provider`. Events that map to a canonical provider
therefore carry `account_id="default"`, `account_source="default"`, and the server holds them
in `pending_usage_events` rather than polluting a labeled account.

1. **Native identity.** `EventIngestor._native_identity_account()` stamps the event with the
   account the host's own collector reported for that provider (`account_source="local"`)
   when exactly one current, enabled, non-rejected credential on this host answers. Operator
   tags always win and archived accounts take no new usage. Only providers whose credential
   carries an identity can resolve this way (Anthropic, xAI); `related_account_provider_ids()`
   mappings are not consulted.
2. **Operator assignment.** Unresolved events appear in Fleet as "Unassigned usage"; assigning
   an account creates a `credential_tags` record scoped to this sidecar. Later cycles receive
   it via `/fleet/config` (`server_account_tag_hints`) and events carry `account_source="tag"`.
   The account list also offers archived accounts and related-provider accounts (for example
   Gemini usage to an Antigravity account, via `_RELATED_ACCOUNT_PROVIDERS` in
   `app/services/account_identity.py`). Hints can be stale for about 10 minutes.
3. **Unmapped providers** (`hermes-<billing_provider>`) keep the host's Hermes label.

## Canonical provider mapping

`scripts/sidecar_pkg/hermes_providers.py` maps `billing_provider` (plus model and base-URL
heuristics when it is missing or generic, such as kimi models and `:free` models) to a
`provider_id`. Main entries:

| Hermes `billing_provider` | `provider_id` |
|---|---|
| `kimi-coding`, `kimi` | `kimi_coding` |
| `minimax`, `minimax-oauth`, `minimax-coding-plan` | `minimax` |
| `opencode-go`, `opencode-zen`, `opencode` | `opencode` |
| `opencode-free` | `opencode-free` |
| `openrouter` | `openrouter` |
| `deepseek`, `deepseek-api` | `deepseek` |
| `anthropic`, `gemini`, `ollama`, `ollama-cloud` | same name |
| `xai`, `xai-oauth`, `xai-api` | `xai` |
| anything else | `hermes-<billing_provider>` |

## Troubleshooting

| Symptom | Fix |
|---|---|
| No events | Check that `state.db` exists under the discovery paths above and has sessions, and that the sidecar log shows Hermes collection. The watermark may already cover the sessions; delete `hermes_watermark.jsonl` to re-scan (the `.migrated` marker prevents the legacy baseline from being restored). |
| Events in "Unassigned usage" | Expected for providers with no native identity (MiniMax, Kimi, Ollama, OpenRouter, DeepSeek). Assign once in Fleet; later events follow the tag. |
| Old events under `hermes-*` ids | `scripts/reclassify_hermes_pending.py` retags pending and legacy rows to canonical providers (dry-run first). |
| Gemini events unpriced after redirect to Antigravity | Hermes sends raw Gemini model ids that do not match Antigravity price rows; cost falls back to Hermes' reported value. |

## Related files

| File | Purpose |
|---|---|
| `scripts/sidecar_pkg/event_extractors/hermes.py` | Event parser |
| `scripts/sidecar_pkg/hermes_providers.py` | Billing-provider resolution |
| `scripts/sidecar_pkg/hermes_watermark.py` | Discovery, watermark log, compaction, pruning |
| `scripts/sidecar_pkg/canonical_providers.py` | Shared canonical provider map |
| `scripts/sidecar_registry_overlay.json` | Sidecar-only provider entry |
| `scripts/reclassify_hermes_pending.py` | Reclassify legacy Hermes pending events |
| `app/services/event_ingestor.py` | Holding back and native-identity resolution |
| `app/services/credential_sources.py` | `sidecar_native_accounts()` |
