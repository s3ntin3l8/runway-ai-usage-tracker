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
| `root.1.19` | Raw model id string (`gemini-pro-default`, `gemini-3-flash-a`, `claude-sonnet-4-6`, …) — authoritative when present |
| `root.1.20` | Repeated KV metadata (`used_claude`, `used_claude_conservative` — consulted only when raw is empty) |
| `root.1.21` | Display name (`Gemini 3.1 Pro (High)`) |

Workspace path (→ `cwd`) comes from `trajectory_metadata_blob` table, field 7, as a `file://` URI.

**Model normalization** (`_normalize_ag_model`):

The raw model id (f1.19) is authoritative whenever present. Claude slugs collapse to their family (`claude-sonnet-4-6` → `claude-sonnet`); Gemini and other non-claude slugs ignore the `used_claude*` KV flags (they latch per-conversation once Claude is touched and otherwise stamp Gemini-raw turns as `claude-opus`). Flags are only consulted when raw is empty (defensive — never observed with a flag set). Family + minor version are then resolved from the raw id and display name (a 3.x minor from either field wins, display first; otherwise display preferred for the minor). Gemini 3.x minor versions get their own cost bucket when seeded; unseeded 3.x minors clamp to the major bucket; flash-lite stays major-only; non-family raw ids pass through verbatim.

| Condition | `model_id` |
|---|---|
| raw starts with `claude` + `sonnet` | `claude-sonnet` |
| raw starts with `claude` (opus, haiku, other unseeded tiers) | `claude-opus` (accepted overbill vs $0 — issue #302) |
| empty raw, `used_claude_conservative=true` | `claude-opus` |
| empty raw, `used_claude=true` (no conservative) | `claude-sonnet` |
| family flash-lite, 3.x signal | `flash-lite-3` (never versioned) |
| family flash/pro, seeded 3.x minor (flash 3.5–3.8, pro 3.1) | `flash-3.5` / `flash-3.6` / `flash-3.7` / `flash-3.8` / `pro-3.1` |
| family flash/pro, 3.x minor without its own pricing row | `flash-3` / `pro-3` (clamped) |
| family flash/pro, major-3 only (no minor) | `flash-3` / `pro-3` |
| family flash/pro, no 3.x signal | bare `flash` / `pro` / `flash-lite` |
| non-family raw (`gpt-oss`, …) | raw id verbatim |
| empty raw, display has family + minor | versioned/bare family from display |
| empty raw otherwise | `unknown` |

**Effort** (`_extract_ag_effort`): a trailing `(Low)` / `(Medium)` / `(High)` on the display name is lowercased into `usage_events.effort`. `(Thinking)` and missing suffixes → `None`.

**Since-watermark:** DBs are filtered by file mtime `> since`. No per-row timestamp; DB mtime is used as approximate event timestamp (spread by `row_idx × 1ms` for stable ordering). Server deduplicates by `event_id = "<conversation_id>|gen_<idx>"`.

**Backfill:** rows ingested before minor-version preservation are repaired by `scripts/reclassify_antigravity_models.py` (run with the server stopped), then repriced via `scripts/recost_events.py --provider antigravity`.

## Pricing

Antigravity events use `provider_id="antigravity"` pricing rows (independent of the `gemini`/`anthropic` rows). Seeded in `app/services/pricing_seed.py`:

| `model_id` | Rate basis |
|---|---|
| `flash-3.5` | Gemini 3.5 Flash — $1.50 / $9.00 / $0.15 |
| `flash-3.6` | Gemini 3.6 Flash — $1.50 / $7.50 / $0.15 |
| `flash-3.7` | Gemini 3.7 Flash — $0.75 / $3.75 / $0.075 |
| `flash-3.8` | Gemini 3.8 Flash — $0.75 / $3.75 / $0.075 |
| `pro-3.1` | Gemini 3.1 Pro — $2.00 / $12.00 / $0.20 |
| `pro-3`, `flash-3`, `flash-lite-3` | Standard Gemini tier (mirrors gemini 3.x rows) |
| `pro` | Bare Pro (no version) — $1.25 / $10.00 / $0.125 |
| `flash` | Bare Flash (no version) — $0.30 / $2.50 / $0.03 |
| `flash-lite` | Bare Flash-Lite (no 3.x) — $0.10 / $0.40 / $0.01 |
| `gemini-default` | Placeholder with no family in display — billed at 3.5 Flash rates |
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
| `scripts/reclassify_antigravity_models.py` | One-shot repair for pre-version-preservation rows |
| `scripts/recost_events.py` | Reprice after pricing-seed changes (`--provider antigravity`) |
| `app/services/pricing_seed.py` | Antigravity pricing rows |
