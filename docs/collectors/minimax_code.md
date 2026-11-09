# MiniMax Code

**File:** `scripts/sidecar_pkg/event_extractors/minimax_code.py`

Sidecar-only extractor that reads the MiniMax Code runtime's own usage ledger (SQLite) and
pushes one usage event per API request. It never produces quota cards: those come from the
server collector, see [minimax.md](minimax.md).

## At a glance

- **Sidecar only.** The server never reads the ledger. `minimax_code` exists as a sidecar
  overlay entry in `scripts/sidecar_registry_overlay.json` (`add_providers`), so the server
  does not list it as a provider.
- **Scheduling id vs provider.** The ledger is collected under the scheduling id
  `minimax_code` (every poll cycle, even on a host with no MiniMax account), but events carry
  `provider_id="minimax"` and `source_app="minimax_code"`. A host that also has a MiniMax
  account is scheduled for both ids; both dispatch the same extractor, so each store is read
  once per cycle.
- **Events only:** `data_source`/`input_source` do not apply (no cards). Events land on the
  MiniMax provider; see [Attribution](#attribution).

## Credentials

None. The ledger is a local file the runtime writes.

| Source | Where | Runs in | Notes |
|---|---|---|---|
| Runtime store | `~/.minimax/v2/sqlite/runtime-state.sqlite` | sidecar | Opened read-only (`?mode=ro`); same path on Linux, macOS and Windows (`%USERPROFILE%`) |
| `MINIMAX_CODE_HOME` | env | sidecar | Overrides the default data dir `~/.minimax` |

The overlay's keyless file rule points at the runtime's small `config.yaml`, never at the
ledger, so the credential scan does not touch the large SQLite file.

## Where the ledger lives

`minimax_code_roots.py` discovers every store on the host, not just the first. Stores are
deduplicated by resolved path (a legacy `~/.mavis` symlink to `~/.minimax` is read once).

| Candidate | Meaning |
|---|---|
| `MINIMAX_CODE_HOME`, `MINIMAX_DATA_DIR`, `MAVIS_DATA_DIR` | Data-dir overrides |
| `__MAVIS_RUNTIME_DATA_DIR`, `__MAVIS_PARENT_DATA_DIR` | Internal overrides set by the runtime's supervisor |
| `~/.minimax-<profile>` | Profile-scoped install next to the default one |
| `~/.mavis`, `~/.mavis-<profile>` | Legacy basename |
| `MiniMax Agent` / `MiniMax Code` app-data dirs | Electron profile dirs (`%APPDATA%` on Windows); probed in case of a rename, they do not hold the ledger |

A missing, locked or schema-drifted store is skipped: no events and no error card.

## Usage events (sidecar)

| Table | Meaning |
|---|---|
| `local_runtime_token_usage` | One row per API request (a turn issues many). `id` is autoincrement, `ts` is ms, `model` is always NULL |
| `local_runtime_message_rows` | One row per message; `data_json` carries `usage`, `finish_reason`, `context_usage_telemetry.model` |
| `local_runtime_sessions` | One row per session: `workspace_dir`, `title`, `extra_data_json` (`effectiveModel`) |

Table names and limits are in `minimax_code_schema.py`; the session, message and attribution
reads are in `minimax_code_ledger.py`.

- **Tokens** are the row's own per-request values, never summed or differenced.
- **Event id** is `mcode:<session>:<turn>:<ts>:<input>:<output>:<cache_read>`, derived from the
  ledger row alone. It must not depend on the message-row join: that row can arrive after the
  request, and a join-dependent id would change between attempts.
- **Model** comes from the first source that names one: the matched message row's
  `context_usage_telemetry.model`, the session's `messages.jsonl`, the session's
  `effectiveModel` (the `minimax/` prefix is stripped so it matches the pricing key), then
  `llm-call.json`. The message row is joined on
  `(session_id, turn_id, input_tokens, output_tokens, cache_read)`.
- **Turns.** The ledger's `turn_id` is pushed, so the Sessions view counts turns, not requests,
  for a session's Messages column and the Agents card. Aggregate views still count requests.
- **Plan-review turns** (`turn_id` starting `mavis-internal:plan-review:`) are real quota use
  and are included, tagged `subagent_type="Plan"`.
- **Background tasks** run as child sessions (`parent_session_id`, `agent_name`, `purpose`).
  Their rows are filed under the parent session, tagged `subagent_type` from the agent name
  (for example `Explore`), and `raw_json` keeps `source_session_id`, `parent_session_id`,
  `agent_name` and `purpose`. The event id keeps the child's own session id. If the parent is
  not in the store the rows keep their own session and are only tagged; the parent-link
  columns are optional for older runtimes.
- **Session names.** `parse_minimax_code_session_names` emits `local_runtime_sessions.title` as
  `generated` name records (background-task sessions are named through their parent).
- **Cost.** `cost_usd` is `None` (the ledger writes 0 for a subscription), so the server prices
  events notionally from the API rates. See
  [collection_logic.md](../collection_logic.md) ("MiniMax Code pricing: no free/paid signal").

## Cursor

The extractor keeps its own id-based cursor in `minimax_code_watermark.json` next to the
sidecar state (`~/.config/runway/sidecar/`, `%APPDATA%\runway\sidecar\` on Windows), keyed by
the resolved database path. `ts` is not monotonic across interleaved sessions, so a timestamp
cursor would silently drop rows; `id` is monotonic per file.

- A store seen for the first time starts from the bootstrap window (`SIDECAR_BOOTSTRAP_DAYS`,
  default 90), applied once on the id axis. Losing the cursor file re-imports that window;
  the server's `(provider_id, event_id)` uniqueness makes the re-push a no-op.
- The cursor advances only after the server confirms the ingest, so a failed push re-sends.
- A row that cannot become an event (unreadable timestamp, colliding id, schema-rejected
  payload) stops the cursor in front of it and is retried each cycle. After three attempts it
  is abandoned and logged at `ERROR`. The cursor file keeps the last 100 abandoned ids per
  store.
- A cycle reads at most 5000 rows per store.
- A rebuilt store (the id axis restarts) resets the cursor; identical requests keep their
  event id and deduplicate.

## Attribution

The app's login carries no email, so events ship under account `"default"` and wait in
Untagged Credentials. With exactly one enabled, labeled MiniMax account the server sends a
`provider:minimax` auto-hint and the sidecar stamps events with that account, landing them on
the quota card. With several accounts, tag the origin explicitly.

## Checking a host

Every sidecar build has a read-only probe. It opens each store, runs the real extractor and
prints a verdict without committing the cursor:

```bash
runway-sidecar-cli --probe-minimax-code        # Linux; use the app binary on macOS / Windows
runway-sidecar-cli --probe-minimax-code --json
```

It reports the probed roots, each store, schema status, ledger counts, rows a first run would
not import, plan-review rows, per-model counts, cursor state (skipped and abandoned counts) and
what the next cycle would push. Exit code 0 means a store was found and understood; 1 means
nothing to read or the schema drifted. On windowed builds with no console it writes
`minimax_code_probe.txt` next to the sidecar config. See [sidecar.md](../sidecar.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| No MiniMax Code usage appears | Run the probe. "stores found: 0" means the data dir is elsewhere: set `MINIMAX_CODE_HOME`. A schema failure means the runtime changed its tables. |
| Probe: rows "not imported" | The first-run window excluded older rows; raise `SIDECAR_BOOTSTRAP_DAYS` and remove the cursor file. |
| Probe: abandoned requests > 0 | Those rows never became events; find the `ERROR` log line for the row id. |
| Events sit in "Unassigned usage" | Expected with no single labeled MiniMax account; assign or tag the origin once. |
| Background tasks show as their own conversations | Rows collected before parent linking: the `minimax_background_sessions` Data Health check moves rows that carry provenance; run `scripts/link_minimax_background_sessions.py --dry-run` then `--apply` on the host for the rest (see [data-health.md](../data-health.md)). Do not run it against a live DB. |
| Costs look non-zero for a plan | Intended: notional API-rate pricing, not billing. |

## Related files

| File | Purpose |
|---|---|
| `scripts/sidecar_pkg/event_extractors/minimax_code.py` | Parser, cursor, event construction |
| `scripts/sidecar_pkg/minimax_code_schema.py` | Table names and scan limits |
| `scripts/sidecar_pkg/minimax_code_roots.py` | Store discovery |
| `scripts/sidecar_pkg/minimax_code_ledger.py` | Session, message and attribution reads |
| `scripts/sidecar_pkg/minimax_code_probe.py` | Read-only probe |
| `scripts/sidecar_registry_overlay.json` | Overlay entry (`add_providers.minimax_code`) |
| `scripts/link_minimax_background_sessions.py` | Backfill of background-task attribution |
| `app/services/data_health/checks/minimax_background_sessions.py` | Data Health check for the same |
| `app/services/maintenance/minimax_background_sessions.py` | Plan/apply repair behind the check |
