# Antigravity Collector

Runway collects Antigravity CLI quota gauges server-side via Google's Cloud Code Assist API, and
extracts per-message token/cost events sidecar-side from the local conversation SQLite databases.

## Architecture

| Component | Where it runs | What it does |
|---|---|---|
| `AntigravityCollector` (server) | `app/services/collectors/antigravity.py` | Fetches 4 quota gauge cards from the Cloud Code Assist API |
| Event extractor (sidecar) | `scripts/sidecar_pkg/event_extractors/antigravity.py` | Parses `gen_metadata` protobuf blobs from conversation DBs |
| OAuth mixin (server) | `app/services/collectors/antigravity_oauth.py` | Reads / caches the agy OAuth token |
| Sidecar credential rule | `scripts/sidecar.py` `__REGISTRY__["antigravity"]` | Ships the OAuth token to the server in multi-host topology |

## Quota Collection

**Source:** `POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary`

**Two-step recipe:**
1. `POST .../v1internal:loadCodeAssist` with `{"metadata": {"ideType": "ANTIGRAVITY"}}` → get `cloudaicompanionProject` id
2. `POST .../v1internal:retrieveUserQuotaSummary` with `{"project": "<id>"}` → quota summary

**Required header:** `User-Agent: antigravity/cli/1.0.9 linux/amd64` (Google gates this endpoint on the agy UA; omitting it returns 403).

**Output:** 4 cards — 2 quota pools × 2 windows:

| Pool | Windows | `window_type` |
|---|---|---|
| Gemini Models (Flash, Pro) | Weekly, 5-hour | `weekly`, `5h` |
| Claude and GPT models | Weekly, 5-hour | `weekly`, `5h` |

Each card carries `pct_used = round((1 − remainingFraction) × 100, 4)`, `reset_at` from `resetTime`, and `quota_pool_id = "antigravity:<pool_family>:<window>"`.

**Credentials:** `~/.gemini/antigravity-cli/antigravity-oauth-token`
```json
{"auth_method": "consumer", "token": {"access_token": "…", "refresh_token": "…", "expiry": "ISO8601"}}
```
agy refreshes this file on each CLI invocation. Runway reads it fresh on every poll; it cannot refresh the token independently (no `client_id` in the file). In multi-host topology the sidecar ships the token via the credential registry rule.

## Per-Message Token Events (Sidecar)

**Source:** `~/.gemini/antigravity-cli/conversations/<uuid>.db`, table `gen_metadata`

Each row is one assistant turn holding a protobuf blob. Confirmed field mapping:

| Proto path | Meaning |
|---|---|
| `root.1.4.2` | `tokens_input` (per-turn prompt tokens) |
| `root.1.4.3` | `tokens_output` (per-turn completion tokens) |
| `root.1.4.1` | `tokens_cache_read` |
| `root.1.4.5` | Cumulative total — **not used** (monotonic) |
| `root.1.19` | Raw model id string (`gemini-pro-default`, `gemini-3-flash-a`, …) |
| `root.1.20` | Repeated KV metadata (`used_claude`, `used_claude_conservative`) |
| `root.1.21` | Display name (`Gemini 3.1 Pro (High)`) |

Workspace path (→ `cwd`) comes from `trajectory_metadata_blob` table, field 7, as a `file://` URI.

**Model normalization** (`_normalize_ag_model`):

| Condition | `model_id` |
|---|---|
| `used_claude_conservative=true` | `claude-opus` |
| `used_claude=true` | `claude-sonnet` |
| raw contains `flash` + `lite`, 3.x display | `flash-lite-3` |
| raw contains `flash`, 3.x display | `flash-3` |
| raw contains `pro`, 3.x display | `pro-3` |

**Since-watermark:** DBs are filtered by file mtime `> since`. No per-row timestamp; DB mtime is used as approximate event timestamp (spread by `row_idx × 1ms` for stable ordering). Server deduplicates by `event_id = "<conversation_id>|gen_<idx>"`.

## Pricing

Antigravity events use `provider_id="antigravity"` pricing rows (independent of the `gemini`/`anthropic` rows). Seeded in `app/services/pricing_seed.py`:

| `model_id` | Rate basis |
|---|---|
| `pro-3`, `flash-3`, `flash-lite-3` | Standard Gemini tier (mirrors gemini 3.x rows) |
| `claude-opus` | Official Claude Opus 4.x API pricing |
| `claude-sonnet` | Official Claude Sonnet 4.x API pricing |
| GPT-OSS 120B (`unknown`) | Unpriced — cost defaults to $0 |

## Setup

No configuration needed when running the sidecar on the same host as `agy`. The sidecar discovers `~/.gemini/antigravity-cli/antigravity-oauth-token` automatically. Run `agy` at least once to create the token file; agy keeps it refreshed on each invocation.

For multi-host (server + remote sidecar), the sidecar ships the OAuth token to the server via the credential registry rule; the server reads it from the token cache.

## Troubleshooting

### No quota cards
- Verify `~/.gemini/antigravity-cli/antigravity-oauth-token` exists and is recent (run `agy` once).
- Check server logs: `[antigravity] collect_via_api failed` with status or error detail.
- The token expires; agy refreshes it on its next run. If Runway polls before agy runs again it returns an Error Card — it will self-heal on the next successful poll.

### No token events / zero cost
- Events only appear after `agy` conversations: check `~/.gemini/antigravity-cli/conversations/` for `*.db` files.
- Cost is $0 for `unknown`/GPT-OSS model ids — this is expected.

## Related Files

| File | Purpose |
|---|---|
| `app/services/collectors/antigravity.py` | Server collector entry point |
| `app/services/collectors/antigravity_api.py` | `retrieveUserQuotaSummary` API mixin |
| `app/services/collectors/antigravity_oauth.py` | OAuth token read/cache mixin |
| `app/core/config.py` | `ANTIGRAVITY_OAUTH_PATH` setting |
| `scripts/sidecar_pkg/event_extractors/antigravity.py` | Conversation DB parser |
| `scripts/sidecar.py` | Sidecar credential rule + event dispatch |
| `app/services/pricing_seed.py` | Antigravity pricing rows |
