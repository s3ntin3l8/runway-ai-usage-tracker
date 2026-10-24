# MiniMax Collector

**File:** `app/services/collectors/minimax.py`

MiniMax coding-plan quota collector.

## Overview

- **Collection Strategy**: api (REST) only — no fallback tier. The HTML coding-plan page
  (`platform.minimax.io/user-center/payment/coding-plan`) is a client-rendered SPA and can't
  be scraped, so a former regex-based fallback was retired.
- **Cards**: Two cards per poll — a `session` (5h rolling) card and a `weekly` card. There is
  no monthly window.
- **Authentication**: `MINIMAX_API_KEY`

## Data Source

### api (Coding Plan API)
**Endpoint:** `https://api.minimax.io/v1/coding_plan/remains` (default — international host).
Set `MINIMAX_HOST=minimaxi.com` to switch to the China host
(`https://api.minimaxi.com/v1/coding_plan/remains`). `MINIMAX_HOST` must be a **bare domain**
— the collector prefixes it with `api.` (and, for the dashboard link, `platform.`) itself.
A value that arrives with a scheme is normalised to its host, so it is never pasted into
the middle of a URL.
**Auth:** Bearer token.

**Response** (real payload, one entry per quota bucket in `model_remains[]` — not one per
billable model; `"general"` covers the text/coding models this collector surfaces, `"video"`
is a separate entitlement it doesn't):

```json
{
  "model_remains": [
    {
      "model_name": "general",
      "start_time": 1788361200000, "end_time": 1788379200000, "remains_time": 4692260,
      "current_interval_total_count": 0, "current_interval_usage_count": 0,
      "current_interval_status": 1, "current_interval_remaining_percent": 100,
      "weekly_start_time": 1788134400000, "weekly_end_time": 1788739200000,
      "current_weekly_total_count": 0, "current_weekly_usage_count": 0,
      "current_weekly_status": 1, "current_weekly_remaining_percent": 100
    },
    {
      "model_name": "video",
      "current_interval_status": 3,
      "...": "..."
    }
  ],
  "base_resp": { "status_code": 0, "status_msg": "success" }
}
```

Field notes (undocumented by MiniMax; derived from observation of a live account):

- `*_remaining_percent` is **remaining**, not used — the collector inverts it
  (`pct_used = 100 - remaining_percent`) when `total_count` is still 0 (a fresh/unused plan).
  Once `total_count > 0`, `pct_used = usage_count / total_count * 100` takes precedence.
- `current_interval_status` (and `current_weekly_status`) bundles two different things.
  `{1, 2}` means "you own this bucket" — `1` = available, `2` = exhausted (0% remaining, observed
  live once real traffic depleted the "general" bucket); both are shown. `3` (observed on the
  `"video"` entry, always 100% remaining regardless of usage) means "not part of your plan" and is
  excluded. MiniMax doesn't document the enum, so this is observed behavior, not a documented
  contract — an earlier version of this collector treated status `2` the same as `3` and silently
  hid the card at 0% remaining, exactly when a user most needs to see it.
- `end_time` / `weekly_end_time` (ms epoch) map directly to `reset_at` — verified against
  `remains_time` (`remains_time == end_time - now`), so there's no need to reconstruct from it.
- The weekly block (`weekly_start_time` / `weekly_end_time` / `current_weekly_*`) is **identical
  across every `model_remains` entry** — one account-level weekly quota, not per-model — so the
  collector emits exactly one weekly card, taken from the first entitled entry.
- `/v1/token_plan/remains` is a newer path MiniMax documents on the same host; this collector
  still uses `/v1/coding_plan/remains`, which is confirmed working.

## Output Format

Two cards, `window_type="session"` (5h) and `window_type="weekly"`:

```python
{
    "service_name": "MiniMax",
    "icon": "🤖",
    "remaining": "90.0%",
    "unit": "capacity",
    "reset": "32m",
    "health": "good",
    "pace": "Sustainable",
    "detail": "40/400 prompts (5h) [API]",
    "used_value": 40.0,
    "limit_value": 400.0,
    "unit_type": "requests",
    "pct_used": 10.0,
    "window_type": "session",
    "model_id": None,
    "reset_at": "2026-09-02T20:00:00+00:00",
    "tier": None,
    "data_source": "api",
    "input_source": "config",
    "usage_url": "https://platform.minimax.io/user-center/payment/coding-plan",
    "updated_at": "2026-09-02T19:27:00+00:00",
}
```

`tier` is always `None` — MiniMax doesn't return a plan/tier name, and third-party sources
disagree on the vocabulary for this product (Plus/Max/Ultra vs. Starter/Pro/Max). The allowance
is surfaced in `detail` instead once `current_interval_total_count`/`current_weekly_total_count`
start reporting real numbers (`0/0` on a fresh, unused subscription — displayed as `"—/—"`).

## Pricing (notional cost)

`app/services/pricing_seed.py` seeds pay-as-you-go API rates (USD/Mtok) for MiniMax models, so
Coding-Plan usage (subscription — $0 actual cost) gets the same "what this would have cost on
the API" figure that Claude subscription usage gets against Claude API rates:

| Model | Input | Output | Cache read | Confidence |
|---|---|---|---|---|
| `MiniMax-M3.1-Flash` | $0.15 | $0.60 | $0.015 | High — MiniMax's own pricing page (checked 2026-10-20) |
| `MiniMax-M3` | $0.30 | $1.20 | $0.06 | High — cross-checked against two sources |
| `MiniMax-M2.7` | $0.30 | $1.20 | $0.06 | Medium |
| `MiniMax-M2.5` | $0.30 | $1.20 | $0.03 | Medium — one source instead quotes $0.27/$0.95 |
| `MiniMax-M2` | $0.255 | $1.02 | $0.03 | Low — single source, not cross-checked |

M3 has an undocumented pricing cliff above 512K input tokens (doubles to $0.60/$2.40, cache
read $0.12); this isn't modeled per-event (the schema has no context-length tiering), so a
>512K-token call will undercount. M3.1-Flash has no published tier split — MiniMax lists it
flat, outside the Standard/Priority tabs that carry M3's context-length tiers — so a single
row covers every call length.

The legacy rows (M2/M2.5/M2.7) are kept so historical events keep their original figure, but
none of them is listed on MiniMax's pricing page any more: M2 is retired, and M2.5/M2.7 are
retired on 2026-11-18 (they were closed to new users on 2026-10-19, when M Plan and Token Plan
traffic on them was automatically rerouted to M3.1 Flash). Their rates came from third-party
sources rather than MiniMax, so treat them as a rough figure rather than a list price.

Rows are keyed on the canonical id (`MiniMax-M3.1-Flash`), so OpenCode's lowercase
`minimax-m3.1-flash` spelling prices through the cost calculator's case-insensitive fallback.
The `-free` spellings (`minimax-m3.1-flash-free`, `MiniMax-M3.1-Flash-free`) stay deliberately
unpriced — that is OpenCode's free route, billed at $0 on purpose, not a missing seed row.
The cost calculator skips its segment-trim fallback for free-tier ids, so a `-free` id can
never reach a paid sibling row and be billed at list price.

## OpenCode traffic

MiniMax coding-plan usage driven through [OpenCode](opencode.md) is folded into these same
cards rather than showing up as a separate `opencode-minimax-coding-plan` entry. See
`_OC_CANONICAL_MAP` in `scripts/sidecar_pkg/event_extractors/opencode.py` — events with
`providerID: "minimax-coding-plan"` are retagged to `provider_id="minimax"`,
`account_id="default"` (matching this collector's own account identity, since MiniMax is
API-key-only and has no account email) at ingest, with their logged $0 cost dropped so the
server prices them from the table above instead.

### OpenCode CLI auto-discovery

If you have the opencode CLI installed and you've configured a MiniMax Coding Plan provider
there, Runway auto-discovers the key from
`~/.local/share/opencode/auth.json["minimax-coding-plan"].key` (or
`~/.opencode/auth.json`) on every host that runs a sidecar. No
`MINIMAX_API_KEY` env var or UI paste needed — the same key opencode uses for
its own `minimax-coding-plan` backend lights up this collector's quota card.

The discovered key's origin is key-scoped exactly like opencode's own
(`path:…/auth.json#<fingerprint>`, see *Key-scoped origins* in
[opencode.md](opencode.md)), so two hosts — or a rotated key — can never
share one operator tag. Paste the same key into Providers → minimax and the
server answers `provider:minimax#<fingerprint>` on the next sidecar cycle.

Already-ingested events under the old `opencode-minimax-coding-plan` id need a one-time
migration, with the server **stopped** (SQLite is single-writer) and `APP_HOST=127.0.0.1`:

```bash
# 1. Start the server once first if this is a fresh DB — seed_pricing_table() only
#    runs inside init_db() at startup, so the MiniMax pricing rows above don't exist
#    until the server has started at least once. Then stop it again before migrating.
# 2. Scope the rescan to just the MiniMax fold-in (skips the much larger legacy
#    opencode/opencode-free backlog, which reclassify_opencode_providers.py can also
#    migrate, but that's a separate, unrelated cleanup):
python scripts/reclassify_opencode_providers.py \
    --providers opencode-minimax-coding-plan --apply
# 3. Price the retagged events from the table above:
python scripts/recost_events.py --provider minimax
# 4. Clear the stale pre-fix gauge cards (see scripts/cleanup_minimax_monthly_cards.py):
python scripts/cleanup_minimax_monthly_cards.py --apply
# 5. Restart the server.
```

## MiniMax Code traffic

MiniMax Code — the runtime behind this agent (the `mavis` agent of the MiniMax Code CLI and
desktop app) — keeps its **own usage ledger** on disk. The sidecar reads it directly, so the
MiniMax Code sessions you run locally show up as usage on the MiniMax cards instead of only
showing up as a quota gauge.

The extractor lives in `scripts/sidecar_pkg/event_extractors/minimax_code.py` and pushes one
event per **API request** under `provider_id="minimax"` with `source_app="minimax_code"`.

### Where the ledger lives

```
~/.minimax/v2/sqlite/runtime-state.sqlite   (+ -wal, -shm)
```

The runtime resolves its data dir from `join(homeDir ?? homedir(), ".minimax")` — the bundle has
no macOS or Windows branch for it — so the **same path works on Linux, macOS and Windows**
(`%USERPROFILE%\.minimax\v2\sqlite\runtime-state.sqlite` on Windows).

That is only the **default**. The runtime also scopes the dir per profile
(`~/.minimax-<profile>` — the profile comes from the desktop app, the CLI's `--profile` flag or
the supervisor, and the runtime derives one from the current branch when it runs inside its own
repository), honours environment overrides, and used to live in `~/.mavis` before it migrated.
The sidecar therefore reads **every** store it finds, not just the first:

| Where | What |
|---|---|
| `MINIMAX_DATA_DIR`, `MAVIS_DATA_DIR` | The runtime's public data-dir overrides. |
| `__MAVIS_RUNTIME_DATA_DIR`, `__MAVIS_PARENT_DATA_DIR` | The internal ones the daemon supervisor injects into the processes it spawns. |
| `~/.minimax-<profile>` | A profile-scoped install, next to the default one. |
| `~/.mavis`, `~/.mavis-<profile>` | The legacy basename, for a host that never migrated. |
| `~/Library/Application Support/MiniMax Agent` | The macOS desktop app's Electron profile — probed as insurance, but it holds the Chromium profile and never the ledger. |
| `%APPDATA%\MiniMax Code` | The same, on Windows. |

Each store carries its own cursor — the cursor file is keyed by database path — so several stores
advance on their own id axes without interfering. A host that never migrated keeps `~/.mavis` as a
**symlink** to `~/.minimax`; the two are deduplicated by resolved path, so the same requests are
never reported twice.

The store is opened **read-only** (`?mode=ro`) — the runtime owns the file, the sidecar never writes
to it, checkpoints it or vacuums it. (SQLite itself may create the WAL sidecar files `-wal`/`-shm`
next to it when a reader opens a WAL store — that is what any WAL reader does, and it changes
nothing in the database.) A missing store (MiniMax Code never ran here), a locked one or a schema
the runtime has drifted away from all degrade to *skipped source*: no events, no error card.

### Checking a host

Every sidecar build carries a read-only probe that reports what the host's MiniMax Code runtime
store looks like and what the extractor would make of it:

```bash
runway-sidecar-cli --probe-minimax-code        # Linux (CLI and tray)
Runway Sidecar.app/Contents/MacOS/RunwaySidecar --probe-minimax-code   # macOS
RunwaySidecar.exe --probe-minimax-code         # Windows
```

```
MiniMax Code runtime store — read-only probe

  platform            : Linux
  probed roots        : /home/x/.minimax, /home/x/Library/Application Support/MiniMax Agent, …
  stores found        : 1
  store               : /home/x/.minimax/v2/sqlite/runtime-state.sqlite (129.0 MiB)
  store modified      : 2026-10-23T16:47:13.208315+00:00
  schema              : OK
  ledger              : 5934 requests · 96 turns · 10 sessions
  plan-review rows    : 2005
  model MiniMax-M3.1-Flash         : 5000
  cursor              : /home/x/.config/runway/sidecar/minimax_code_watermark.json
  cursor last id      : (none)
  this cycle would push: 5000 request(s) (a cycle reads at most 5000 rows per store)

  RESULT: PASS — the sidecar on this host reports MiniMax Code usage as provider 'minimax' / app 'minimax_code'.
```

The probe opens every store read-only, runs the real extractor and prints its verdict; it
**never commits the cursor**, so running it cannot make the sidecar skip rows. It exits 0 when a
store was found and understood, and 1 when there is nothing to read (no store) or the schema has
drifted. `--json` prints the same report as JSON, with one entry per store under `stores`.

A host can hold more than one store — a profile-scoped install keeps a second ledger next to the
default one. The report names each of them, and the counts are the whole host's, because the
sidecar reads them all.

On a windowed build (the macOS app and the Windows `.exe` have no console of their own) the probe
attaches to the console it was launched from; with no console to attach to it writes the report to
`minimax_code_probe.txt` next to the sidecar's config and says so in a message box.

**Platform status.** The store path is identical on every platform (the runtime resolves it from
`homedir()`), and the probe answers the question on whichever host you run it on:

| Platform | Store path | Verified |
|---|---|---|
| Linux | `~/.minimax/v2/sqlite/runtime-state.sqlite` | Yes — full extraction verified against a live store |
| macOS | `~/.minimax/v2/sqlite/runtime-state.sqlite` | Yes — the probe passes against a live store; 99.97 % of the ledger rows join, the model resolves for every row |
| Windows | `%USERPROFILE%\.minimax\v2\sqlite\runtime-state.sqlite` | Path confirmed from the runtime bundle and from the install that opened this issue; run the probe on a Windows host to confirm the extraction |

A platform that has not been confirmed reports **nothing** rather than wrong numbers: a missing
store yields no events and a drifted schema yields no events.

### What the store holds

| Table | Meaning |
|---|---|
| `local_runtime_token_usage` | One row per **API request** — a turn issues dozens of them. `id` is an AUTOINCREMENT key, `ts` is the request time in ms, `model` is always NULL. |
| `local_runtime_message_rows` | One row per stored message. `data_json` carries `usage`, `tool_calls`, `finish_reason`, `canonical_message_id` and `context_usage_telemetry.model`. |
| `local_runtime_sessions` | One row per session. `workspace_dir` is the working directory, `extra_data_json` carries `effectiveModel` and `effectiveModelThinking.effort`. |

The token columns are Anthropic-style **per-request** values, not deltas:
`raw.totalTokens == input + output + cacheRead + cacheWrite` holds for every row, and
`cache_read` grows within a turn and resets when the context is rebuilt. The events therefore
carry the row's own values as-is — never a sum across a turn and never a delta.

`cost_usd` is pushed as `None`: the runtime writes `0.0` because the M Plan is a subscription, so
the server prices the event from the `MiniMax-M3.1-Flash` row in the table above.

### Why the cursor is an id and not a timestamp

`ts` is **not** globally monotonic — sessions interleave, so the ledger has hundreds of backward
steps. A `ts > since` cursor silently drops rows: simulating this extractor's cadence over a real
store, a timestamp cursor lost **14.3 %** of the rows at a 5-row cadence and 1.4 % even at 100
rows per cycle, while the `id` cursor lost none.

The extractor therefore keeps its own id-based cursor in
`~/.config/runway/sidecar/minimax_code_watermark.json` (`%APPDATA%\runway\sidecar\…` on Windows —
it sits next to the rest of the sidecar's state), keyed by the database path (`id` is only
comparable within one file). That keying is what lets several stores coexist: each advances on
its own id axis. The key is the **resolved** path, so the legacy `~/.mavis` → `~/.minimax`
symlink keeps its cursor, while a data dir that is *moved* or *copied* resolves to a new key and
starts from scratch.

**The floor for a store that is seen for the first time is the bootstrap window**, never the
sidecar's shared timestamp watermark. The watermark says "this provider's history up to T was
pushed" — which is true for the store that was pushed and for nothing else, so it is not a floor
for a store discovered later. It is not a safe floor for the known store either: `ts` is not
globally monotonic, so a timestamp boundary drops the interleaved rows that fall outside it (the
same reason the id cursor exists). The window — the same one the probe uses, so the two cannot
disagree — is applied once, on the **id** axis, and then forgotten. Losing the cursor file
therefore re-imports the window rather than the rows since the last push; the rows are identical,
so the server's `(provider_id, event_id)` uniqueness makes the re-push a no-op.

The cursor advances **only after the server confirms the ingest**: the advance is parked while the
events are on their way and committed — or dropped — once the push is done. A failed push (or a
cycle that dies mid-flight) therefore re-sends the same rows instead of skipping them; the
server's `(provider_id, event_id)` uniqueness makes that re-send a no-op.

A cycle reads at most 5000 rows **per store**, so a host with N stores can emit up to N × 5000
events in one cycle.

A rebuilt store (the id axis restarts) resets the cursor and re-emits. The rebuilt store holds the
requests made after the rebuild — usage that has not been reported yet — and a request that
reappears with the same timestamp and the same usage keeps its event id, so the server deduplicates
it rather than counting it twice.

The event id is derived from the ledger row alone:
`mcode:<session>:<turn>:<ts>:<input>:<output>:<cache_read>`. It must not depend on the join with
the message rows — a request's message row can land in the store *after* the request itself, so a
join-dependent id would change between two attempts at the same row and the server would count the
request twice. Every part of it is content-derived, which also keeps two requests of one turn that
share a millisecond apart (their token counts tell them apart) no matter which batch they land in.

### Model resolution

`local_runtime_token_usage.model` is always NULL, so the model comes from the first source that
names one:

1. the matched message row's `context_usage_telemetry.model`,
2. the session's `messages.jsonl`,
3. the session's `extra_data_json.effectiveModel` (`minimax/MiniMax-M3.1-Flash` — the provider
   prefix is stripped so the id matches the pricing seed key),
4. the session's `llm-call.json`.

The message row is joined on the usage tuple
`(session_id, turn_id, input_tokens, output_tokens, cache_read)`, which matches the ledger's
per-request values against the message's own `usage` block. The history JSONL is **not** used for
usage — it is pruned, so it loses rows — only as a model fallback.

### Internal plan-review turns

Roughly 60 % of the ledger's rows belong to the runtime's internal plan-review sub-agent
(`turn_id` starting with `mavis-internal:plan-review:`). They are real API calls against your
plan quota, so they are included and tagged `subagent_type="Plan"` — the Sessions view renders
them as a separate card, split out of the session's main thread.

### Background-task sessions

The runtime spawns a background task as a **child session**: its own session id, a
`parent_session_id` pointing at the session that owns the work, an `agent_name` naming the agent
(`explore`) and a `purpose` naming the task (`local-background-task:bg_<uuid>`). The desktop app
runs those in the background, so their requests are real calls against your plan quota — but they
belong to the session that spawned them, not to a conversation of their own.

Claude Code models the same work as a **sidechain**: the message carries the *parent's* session id
plus an attribution agent. The extractor reproduces that shape:

- the child's rows are filed under the **parent** session,
- they are tagged `subagent_type="Explore"`, so the Sessions view renders them as the parent's
  **Agents** card instead of listing them as a session of their own,
- their `raw_json` carries the provenance (`source_session_id`, `parent_session_id`, `agent_name`,
  `purpose`), so the child session stays recoverable from the stored row.

Two properties keep this safe:

- **The event id keeps the child's own session id.** It is derived from the ledger row alone, so it
  does not change when the attribution changes: every row collected before this change keeps its
  id, and the server deduplicates the re-push instead of counting it twice.
- **The parent-link columns are optional.** A store written by a runtime that predates them keeps
  working (no attribution, no crash). A parent that is not in the store is never invented — the
  rows keep their own session id and are only tagged.

The **turn** dimension rides along: the extractor pushes the ledger's own `turn_id`, so the Agents
card counts *turns* (one background task = one turn) instead of API requests. A provider that
emits one event per message (Claude Code) carries no turn id, and its numbers are unchanged.

The session's own **Messages** column counts turns the same way, so a session row and its Agents
card speak the same unit: a MiniMax Code session reports the turns you took, not the requests
your turn was made of. The Top Projects table follows. The aggregate views (the provider's
monthly totals, the charts) still count requests — their numbers come from the stored rollups,
and a distinct-turn count cannot be summed incrementally across ingest batches.

Two details worth knowing:

- **It is not retroactive.** Rows collected before the turn id existed carry none, so they keep
  counting as requests until the repair fills their turn id.
- A turn is attributed to **every model it touched** — the same way its tokens are. A turn that
  used two models therefore appears in both per-model buckets while the session row counts it
  once.
- A **turn id names its turn on its own**: the runtime mints them uniquely (a live store holds
  130 distinct turn ids across 18 sessions, none shared). That also keeps a turn's identity
  stable when the repair moves its row from the task's session to the parent.

#### Repairing the rows collected earlier

Rows collected before this change still carry the background task's own session id, so they show up
as a conversation of their own. Move them with:

```bash
python scripts/link_minimax_background_sessions.py --dry-run
python scripts/link_minimax_background_sessions.py --apply
```

The repair reads the runtime stores (read-only), updates the attribution of the rows the server
already holds and **never inserts**: no token count changes and no rollup move — the rollups are
keyed by provider, account and period, never by session. A project's *session count* does drop: a
background task was never a conversation.

Every store on the host is repaired (a profile-scoped install keeps its ledger next to the default
one, and each holds its own background tasks). Pass `--store` to repair specific ones instead.

Three cases worth knowing about:

- A background task whose **parent session is not in the store** keeps its own session id and is
  only *tagged* — the same degradation the extractor applies. The repair tags it too; it does not
  drop it.
- A row still parked in **Untagged Credentials** (`pending_usage_events`) is outside the repair: the
  promotion writes the payload the sidecar sent, so run the repair again after the account is
  assigned. The run is idempotent, so that is enough.
- A plain re-push of the same row deduplicates on its event id, so the repair stands. A re-push
  under a **different account** is a retag: the server refreshes the stored row from the sidecar's
  payload, so a sidecar that has not been upgraded yet puts the row back on the child. Run the
  repair again to heal it.

Run it against the dev DB or a snapshot — never the live prod DB while the server is writing
(SQLite is single-writer).

### Attribution

The MiniMax Code auth bundle carries no email (its record is keyed by an opaque provider id), so
the sidecar has no local identity to derive. The events therefore ship under the `"default"`
sentinel, where they wait in the **Untagged Credentials** dialog — the same path OpenCode-driven
MiniMax traffic takes.

Once the operator has exactly one enabled, labeled MiniMax account, the server ships its
`provider:minimax` auto-hint and the sidecar stamps the events with that account: they land on the
same card as the quota gauge instead of a standalone `"default"` one. A multi-account setup has no
auto-hint (the server refuses to guess) — the operator tags the origin explicitly there.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `MINIMAX_API_KEY` | Yes | MiniMax API key |
| `MINIMAX_HOST` | Optional | Bare-domain host override. Default `minimax.io` (international); set to `minimaxi.com` for the China platform. |
| `MINIMAX_CODE_HOME` | Optional | Data-dir override for the MiniMax Code runtime store. Default `~/.minimax`. The runtime's own overrides (`MINIMAX_DATA_DIR` and friends) are honoured too — see [Where the ledger lives](#where-the-ledger-lives). |

## Sidecar Support

Sidecar uses the same API key. No aggregation needed. See [sidecar documentation](../sidecar.md).

The sidecar additionally reads the **MiniMax Code runtime ledger** on the host and pushes those
requests as usage events under this provider — see
[MiniMax Code traffic](#minimax-code-traffic) above. That path needs no API key at all: the
ledger is a local file the runtime writes.

## Troubleshooting

### "Missing API key" error
**Fix:**
1. Get key from https://api.minimax.io (international) or https://api.minimaxi.com (China)
2. `export MINIMAX_API_KEY="minimax-..."`
3. If you registered on the China platform, also set `MINIMAX_HOST=minimaxi.com`.

### API connection failed
**Cause:** Network error or invalid API key.
**Fix:** Verify your internet connection and API key validity.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/minimax.py` | Main collector |
| `app/services/pricing_seed.py` | Notional API pricing for Coding-Plan usage |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Folds OpenCode-driven MiniMax traffic into this provider |
| `scripts/sidecar_pkg/event_extractors/minimax_code.py` | Reads the MiniMax Code runtime ledger and pushes its requests as events |
| `scripts/sidecar_pkg/minimax_code_probe.py` | The read-only `--probe-minimax-code` host check |
| `scripts/link_minimax_background_sessions.py` | Files the background-task rows collected before the parent link under their parent |
| `app/services/maintenance/minimax_background_sessions.py` | The repair's plan/apply pair (no runtime-store access of its own) |
| `tests/unit/test_event_extractor_minimax_code.py` | Extractor coverage (join, model fallbacks, id cursor, drift) |
| `tests/unit/test_maintenance_minimax_background_sessions.py` | Repair coverage (moves, never inserts, idempotent) |
| `tests/unit/test_minimax_code_probe.py` | Probe coverage (store found/missing, schema drift, cursor left alone) |
| `tests/integration/test_minimax_code_events_ingest.py` | End-to-end: runtime ledger → fleet ingest → priced MiniMax events |

## References

- **MiniMax (international):** https://www.minimax.io
- **MiniMax (China):** https://www.minimaxi.com
- **API Documentation:** https://api.minimax.io/document/main (or https://api.minimaxi.com/document/main for the China platform)

*Last updated: 2026-10-24*
