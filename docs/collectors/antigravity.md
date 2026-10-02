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
The **access token lives one hour** and only agy can renew it: the file carries no OAuth `client_id`, so neither the server nor the sidecar can refresh it (`_execute_refresh` is a no-op; a 401 just re-reads the cache in case another host pushed a live token — it never logs a refresh, see `REFRESHABLE = False`). Runway reads the file fresh on every poll; in multi-host topology the sidecar ships the token via the credential registry rule, and a sidecar whose local token has **lapsed does not push it at all**, so one machine's dead session cannot poison the shared cache entry another machine keeps fresh.

### Token lifetime & keep-alive

Because the token renews only when agy itself runs, a host without regular agy sessions lapses hourly — four lapses in one day in the 2026-10-02 incident. Keep it fresh with any of:

1. **Sidecar keep-alive (opt-in):** `runway-sidecar-cli --keep-alive` (or `"keep_alive": true` in `config.json`). The sidecar checks the token file every minute and, once the access token has lapsed, runs `agy models` — a metadata call that renews the token in place: **verified 2026-10-02** that it rewrites only the access token and `expiry` (the refresh_token is never rotated), makes no model call, and works even while the access token is already expired. A failed attempt backs off to one retry every five minutes; the thread logs and never takes the sidecar down.
2. **systemd timer (hosts without the sidecar):** run `agy models` on a short interval. agy only rewrites the file once the token has actually lapsed, so a frequent timer stays cheap and bounds the dead-token window to one tick:

   ```ini
   # ~/.config/systemd/user/agy-keepalive.service
   [Unit]
   Description=Renew the agy access token

   [Service]
   Type=oneshot
   ExecStart=%h/.local/bin/agy models
   ```
   ```ini
   # ~/.config/systemd/user/agy-keepalive.timer
   [Unit]
   Description=Renew the agy access token every 10 minutes

   [Timer]
   OnBootSec=5min
   OnUnitActiveSec=10min
   AccuracySec=1min

   [Install]
   WantedBy=timers.target
   ```
   Then `systemctl --user enable --now agy-keepalive.timer`.
3. **A live `agy` session** on the machine — what masked the lapses before keep-alive existed (a long-running session renews on each turn).

> **Keep-alive assumes today's renewal semantics.** The "rewrites only the access token, never the refresh token" property was verified manually (2026-10-02) and cannot be checked by this repo's test suite: before enabling unattended keep-alive against a new agy release, diff the token file's `refresh_token` across one `agy models` renewal (it must not change) and scan the release notes for refresh-token handling — a CLI that starts rotating it would sign every other machine's login out.

While the token is still valid but within 10 minutes of expiry, the sidecar logs a pre-expiry `WARNING` ("run `agy models` … or start the sidecar with `--keep-alive`") — suppressed automatically when keep-alive already owns renewal.

**Multi-machine:** every machine's sidecar pushes its own agy token under the resolved account, and the server keeps the freshest push. The default collector discovers those identity-keyed bundles and fails over between them, so as long as *any* machine holds a live session the quota keeps flowing; Fleet → Token Health shows per-source `auth_failed` / `healthy` state.

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

The raw model id (f1.19) is authoritative whenever present. Claude slugs (including aliases like `anthropic-claude-sonnet-4-6`) collapse to their family (`claude-sonnet-4-6` → `claude-sonnet`); Gemini and other non-claude slugs ignore the `used_claude*` KV flags (they latch per-conversation once Claude is touched and otherwise stamp Gemini-raw turns as `claude-opus`). When raw is empty, the display path runs first; flags are only consulted if that path would return `unknown` (defensive — never observed with a flag set). Family + minor version are then resolved from the raw id and display name (a 3.x minor from either field wins, display first; otherwise display preferred for the minor). Gemini 3.x minor versions get their own cost bucket when seeded; unseeded 3.x minors clamp to the major bucket; flash-lite stays major-only; non-family raw ids pass through verbatim.

| Condition | `model_id` |
|---|---|
| raw contains `claude` + `sonnet` | `claude-sonnet` |
| raw contains `claude` (opus, haiku, other unseeded tiers) | `claude-opus` (accepted overbill vs $0 — issue #302) |
| empty raw + empty/unknown display, `used_claude_conservative=true` | `claude-opus` |
| empty raw + empty/unknown display, `used_claude=true` (no conservative) | `claude-sonnet` |
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

No configuration needed when running the sidecar on the same host as `agy`. The sidecar discovers `~/.gemini/antigravity-cli/antigravity-oauth-token` automatically. Run `agy` at least once to create the token file.

For multi-host (server + remote sidecar), the sidecar ships the OAuth token to the server via the credential registry rule; the server reads it from the token cache.

On hosts where agy sessions are intermittent, enable keep-alive (`--keep-alive` / `"keep_alive": true`) or install the systemd timer — see [Token lifetime & keep-alive](#token-lifetime--keep-alive). Without one of these, the token renews only when agy next runs.

## Troubleshooting

### No quota cards
- Renew the token first: `agy models` (metadata call, no chat, rotates only the access token). Then re-poll.
- A sidecar `WARNING — local token expired … not pushing` means *this* host's session lapsed; the server then serves whichever machine still has a live token — nobody, if all of them lapsed. Fix with `agy models`, a live agy session, or `--keep-alive`.
- A pre-expiry `WARNING — local token expires at … (within 10 min)` is the early version of the same signal: act before quota collection breaks.
- Check server logs: `[antigravity] collect_via_api failed` with status or error detail; `Refreshing … access token` never appears for Antigravity (the server cannot refresh it), so a 401 there means every candidate token was dead.
- Per-source state lives in Fleet → Token Health: `auth_failed` on one source while another shows `healthy` means failover is carrying the account.

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
| `scripts/sidecar_pkg/keep_alive.py` | Optional `--keep-alive` thread (`agy models` renewal) |
| `scripts/reclassify_antigravity_models.py` | One-shot repair for pre-version-preservation rows |
| `scripts/recost_events.py` | Reprice after pricing-seed changes (`--provider antigravity`) |
| `app/services/pricing_seed.py` | Antigravity pricing rows |
