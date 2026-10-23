# Antigravity Collector

Runway collects Antigravity CLI quota gauges server-side via Google's Cloud Code Assist API, and
extracts per-message token/cost events sidecar-side from the local conversation SQLite databases.

## Architecture

| Component | Where it runs | What it does |
|---|---|---|
| `AntigravityCollector` (server) | `app/services/collectors/antigravity.py` | Fetches up to 5 quota gauge cards from the Cloud Code Assist API |
| Event extractor (sidecar) | `scripts/sidecar_pkg/event_extractors/antigravity.py` | Parses `gen_metadata` protobuf blobs from conversation DBs |
| OAuth mixin (server) | `app/services/collectors/antigravity_oauth.py` | Reads / caches the agy OAuth token and the CLI version reported with it |
| CLI version (sidecar) | `scripts/sidecar_pkg/agy_version.py` | Reads the installed `agy --version` and reports it with the token |
| Sidecar credential rule | `scripts/sidecar.py` `__REGISTRY__["antigravity"]` | Ships the OAuth token to the server in multi-host topology |

## Quota Collection

**Source:** `POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary`

**Two-step recipe:**
1. `POST .../v1internal:loadCodeAssist` with `{"metadata": {"ideType": "ANTIGRAVITY"}}` → get `cloudaicompanionProject` id
2. `POST .../v1internal:retrieveUserQuotaSummary` with `{"project": "<id>"}` → quota summary

**Required header:** `User-Agent: antigravity/cli/<version> (aidev_client; os_type=linux; arch=amd64; cl=1002753436; auth_method=consumer)` (Google gates this endpoint on the agy UA; omitting it returns 403).

> **The UA version decides which quota pools come back.** The prefix is the 403 gate, but the
> *version* selects the pools the backend serves — measured 2026-10-20 against
> `daily-cloudcode-pa.googleapis.com` with the same token, project and request body, changing only
> the UA:
>
> | UA version | groups returned |
> |---|---|
> | `< 1.2` | "Claude and GPT models" only |
> | `>= 1.2` | + "Gemini Models" |
> | `>= 1.3` | + "Gemini 4 Argon" (also unlocked at 1.2 by the `aidev_client` marker) |
>
> An outdated UA therefore fails *silently*: the API answers 200 with a subset of the pools and
> Runway reports only the Claude/GPT quota. That is how the Gemini pools went missing — the
> collector still carried agy 1.0.9 from June 2026 while the installed CLI was 1.4.3.
>
> **The version follows the installed CLI.** The CLI lives on the sidecar's host and the server
> never sees it, so the sidecar reports the installed `agy --version` next to the token
> (`scripts/sidecar_pkg/agy_version.py`) and the collector builds the User-Agent from it. The
> report rides along with the credential bundle (`agy_version` in the token card's metadata), so
> every read path — including the "borrow the newest entry" fallback — sees it.
>
> The sidecar caches the read: an hour for a success (the CLI only changes when agy itself is
> updated), a minute for a **failure**. A miss is never cached for the full hour — agy may not
> be on `PATH` yet at boot, or one read may time out, and a stale "unknown" would suppress the
> report (and with it a quota pool) for exactly as long as the problem takes to notice.
>
> The collector keeps a **floor version** (`_UA_FLOOR_VERSION`, the oldest version known to serve
> every pool). It is used when no sidecar reported a version (an older sidecar) and it is never
> dropped below, so an older CLI cannot hide a pool Runway already surfaces. Keep the floor in
> step with the installed agy CLI — a unit test fails if it drops below 1.3 — and treat a missing
> Gemini pool on a paid tier as the signal that the UA fell behind.
>
> The reported value is vetted by a shared validator (`app/core/version.py`): ASCII digits only,
> one to three parts, no non-strings. The value ends up in an HTTP header and httpx encodes
> headers as ASCII, so a Unicode digit would raise there and fail the account's collection.


**Output:** up to 5 cards — 3 quota pools:

| Pool | `variant` | Windows | `window_type` |
|---|---|---|---|
| Gemini Models (Flash, Pro) | `gemini` | Weekly, 5-hour | `weekly`, `session` |
| Claude and GPT models | `frontier` | Weekly, 5-hour | `weekly`, `session` |
| Gemini 4 Argon (paid Pro/Ultra plans only) | `argon` | Weekly | `weekly` |

Each card carries `pct_used = round((1 − remainingFraction) × 100, 4)`, `reset_at` from `resetTime`, and `quota_pool_id = "antigravity:<variant>:<window>"`.

The API's `5h` window is the canonical `session` window. The group display name decides the
pool: "Gemini 4 Argon" is a **separate** paid-plan pool (it carries its own weekly limit) and
must not be folded into the `gemini` pool — both would then share one `latest_usage` slot
(`provider, account, window_type, variant, model_id`) and overwrite each other. The Argon pool
disappears on its own when Google folds it back into the Gemini bucket: the collector returns a
complete snapshot, so a pool that is no longer served is retired.

A group the collector does not know yet gets a **slug of its own name** as its pool (e.g.
"Some Future Pool" → `some-future-pool-<hash>`) plus a server-log warning — never an existing
pool's slot, so a new pool cannot silently overwrite another one. The known pool keys are
reserved: a group whose slug would collide with one (e.g. a bare "Argon") is prefixed
(`unknown-argon-<hash>`). The short hash comes from the group's *resolved* name (its display
name, else its first bucket id), so two groups that differ only in case or punctuation
("Gemini 5 Code" vs "gemini 5-code") still land in different pools. The hash is an id, not a
label: the card shows the readable name ("Some Future Pool").
The known groups are matched by anchored patterns (not loose substrings): a group merely
*containing* "gemini"/"claude"/"gpt" is a new pool, not a member of an existing one. The patterns
tolerate the spellings Google is known to use — a trailing "Pool" ("Gemini Models Pool"), "&"
for "and" ("Claude & GPT models"), a version number on the Argon pool ("Gemini 4 Argon" →
"Gemini 5 Argon") — so a cosmetic rename does not re-key a live pool (which would split its
history and fire the wrong warning). Any other rename shows up as a new, slug-named pool plus
the warning, and the old row is retired with the next complete snapshot. A group without a
display name falls back to its bucket ids, so two unnamed groups cannot land in the same pool
either.

The server logs `Antigravity quota summary is missing the <pool> pool(s) for tier '<tier>'` when
a tier comes back without a pool it should have — the fingerprint of a User-Agent that fell
behind (a 1.2 User-Agent serves the Gemini pool but not the Argon one, which is exactly how
"the Argon quota was never reported" looked). The User-Agent gate is account-independent, so a
free account is covered too — its line is only logged at debug level. The check keys on the
tier being known at all, so a paid tier id Google adds later is covered as well.

**Credentials:** `~/.gemini/antigravity-cli/antigravity-oauth-token` — where agy keeps no token file, the Windows Credential Manager or the macOS login Keychain instead (see below).
```json
{"auth_method": "consumer", "token": {"access_token": "…", "refresh_token": "…", "expiry": "ISO8601"}}
```
The **access token lives one hour** and only agy can renew it: the file carries no OAuth `client_id`, so neither the server nor the sidecar can refresh it (`_execute_refresh` is a no-op; a 401 just re-reads the cache in case another host pushed a live token — it never logs a refresh, see `REFRESHABLE = False`). The server never reads the file: the sidecar reads it on every push and ships the token via the credential registry rule, and a sidecar whose local token has **lapsed does not push it at all**, so one machine's dead session cannot poison the shared cache entry another machine keeps fresh.

The server never touches a credential store: only the sidecar does, and it ships the
token like any other source.

### Where the credential lives

The CLI moved its login out of the file, so the sidecar has to look in the OS credential store the
CLI now uses. The rule is chosen by platform; both carry the same bundle and push the same fields.

| Platform | Where the credential is | How the sidecar reads it |
|---|---|---|
| Linux, macOS (agy < 1.4.3) | `~/.gemini/antigravity-cli/antigravity-oauth-token` | the file rule |
| macOS (agy ≥ 1.4.3) | the login **Keychain**, service `gemini`, account `antigravity` | the Keychain worker, behind a local consent |
| Windows | the **Credential Manager**, target `gemini:antigravity` | a native `advapi32` read (#732) |

On macOS the stored value is wrapped in go-keyring's envelope — `go-keyring-base64:` followed by
base64 — because the CLI writes it through that library; `scripts/sidecar_pkg/keyring_value.py`
unwraps it (both the base64 and the hex envelope) and the rule maps the same three fields the
file rule maps.

The macOS entry's access list trusts only the application that wrote it, so the sidecar cannot
read it on its own: the read goes through the Keychain worker, and the user authorizes it once in
the local Sources UI (**Authorize** raises the macOS "Runway wants to access 'gemini' in your
keychain" prompt; after allowing it, the background read is silent and the grant can be withdrawn
in Keychain Access). Until then the source is off and nothing is read — see
[docs/sidecar-source-authorization.md](../sidecar-source-authorization.md).

The origin is `keychain:gemini:antigravity` on every platform that keeps the credential in an OS
store, so the same Google account is filed under one cache key whichever store it came from. The
bundle the macOS entry carries also has an `id_token` that the file does not; it is deliberately
**not** mapped, because the server derives the account label from an `id_token` when one is
present and a platform-specific mapping would file one account under two keys.

> **The macOS credential follows the negotiated model.** Like every other Keychain-backed
> credential it leaves the host only when the user has granted the source locally, authorized
> the entry, and the device's negotiated scope includes it — see
> [docs/sidecar-source-authorization.md](../sidecar-source-authorization.md).

### Token lifetime & keep-alive

Because the token renews only when agy itself runs, a host without regular agy sessions lapses hourly — four lapses in one day in the 2026-10-02 incident. Keep it fresh with any of:

1. **Sidecar keep-alive (opt-in):** `runway-sidecar-cli --keep-alive` (or `"keep_alive": true` in `config.json`). The sidecar checks the local token every minute and, once the access token has lapsed, runs `agy models` — a metadata call that renews the token in place: **verified 2026-10-02** that it rewrites only the access token and `expiry` (the refresh_token is never rotated), makes no model call, and works even while the access token is already expired. A failed attempt backs off to one retry every five minutes; the thread logs and never takes the sidecar down. The thread reads the token from wherever this OS keeps it — the file on POSIX, the Credential Manager entry on Windows — and `agy models` renews it there, so the loop is the same on every platform.
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

Gemini 4 Argon is priced as one family under its own canonical id and bypasses the tier buckets: the raw id carries a `-trial` suffix (`gemini-4-argon-trial`) that must not survive into the stored `model_id`, so the whole `gemini-4-argon*` family normalizes to `gemini-4-argon` (issue #686). The display name (`Gemini 4 Argon (High)`) is used when the raw id is empty or carries no family of its own; a look-alike such as `gemini-4-argonoid` is a different model and passes through verbatim.

| Condition | `model_id` |
|---|---|
| raw contains `claude` + `sonnet` | `claude-sonnet` |
| raw contains `claude` (opus, haiku, other unseeded tiers) | `claude-opus` (accepted overbill vs $0 — issue #302) |
| empty raw + empty/unknown display, `used_claude_conservative=true` | `claude-opus` |
| empty raw + empty/unknown display, `used_claude=true` (no conservative) | `claude-sonnet` |
| Gemini 4 Argon (`gemini-4-argon*` raw, or `Gemini 4 Argon` display) | `gemini-4-argon` |
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

**Backfill:** rows ingested before minor-version preservation are repaired by `scripts/reclassify_antigravity_models.py` (run with the server stopped), then repriced via `scripts/recost_events.py --provider antigravity`. Rows ingested before the Argon family was recognized are repaired by `scripts/backfill_argon_models.py`, which rewrites the stored `model_id` to `gemini-4-argon` and re-prices the event in the same pass.

## Pricing

Antigravity events use `provider_id="antigravity"` pricing rows (independent of the `gemini`/`anthropic` rows). Seeded in `app/services/pricing_seed.py`:

| `model_id` | Rate basis |
|---|---|
| `gemini-4-argon` | Gemini 4 Argon — $2.00 / $10.00 / $0.10 through 2026-11-30, $4.00 / $20.00 / $0.20 from 2026-12-01 |
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

Run the sidecar on the host where `agy` is logged in (same host as the server or a remote one): it discovers the local token automatically — `~/.gemini/antigravity-cli/antigravity-oauth-token` on POSIX, the `gemini:antigravity` Credential Manager entry on Windows — and ships the OAuth token to the server, which reads it only from its token cache. Run `agy` at least once to create the token. A server with no sidecar has no Antigravity credential (`make dev` alone does not see it; use `make dev-all`).

On macOS with agy 1.4.3 or newer there is no token file: the CLI keeps the credential in the login
Keychain, so the sidecar needs the local consent for the **Antigravity CLI** source before it will
read it — enable the source in the local Sources UI and click **Authorize** once, then allow the
macOS prompt. Until both steps are done the credential is not read at all.

On hosts where agy sessions are intermittent, enable keep-alive (`--keep-alive` / `"keep_alive": true`) or install the systemd timer — see [Token lifetime & keep-alive](#token-lifetime--keep-alive). Without one of these, the token renews only when agy next runs. On macOS the keep-alive reads the expiry from the Keychain entry, so it stays quiet until the source is consented; a CLI older than 1.4.3 still keeps its credential in the token file, and that layout needs no consent.

## Troubleshooting

### No quota cards
- Renew the token first: `agy models` (metadata call, no chat, rotates only the access token). Then re-poll.
- A sidecar `WARNING — local token expired … not pushing` means *this* host's session lapsed; the server then serves whichever machine still has a live token — nobody, if all of them lapsed. Fix with `agy models`, a live agy session, or `--keep-alive`.
- A pre-expiry `WARNING — local token expires at … (within 10 min)` is the early version of the same signal: act before quota collection breaks.
- Check server logs: `[antigravity] collect_via_api failed` with status or error detail; `Refreshing … access token` never appears for Antigravity (the server cannot refresh it), so a 401 there means every candidate token was dead.
- Per-source state lives in Fleet → Token Health: `auth_failed` on one source while another shows `healthy` means failover is carrying the account.

### Only the Claude/GPT quota is shown (Gemini pools missing)
- The API gates the pools it serves on the **User-Agent version** — see the table above. If the
  User-Agent the server sent is older than the agy CLI you run, the response arrives with 200
  but without the Gemini (and Argon) groups, and the dashboard shows just the Claude/GPT pool.
  The version normally follows the CLI (the sidecar reports it with the token), so this points at
  a sidecar that is not reporting one — check the sidecar log for an unrecognised version, and
  at the floor version in `app/services/collectors/antigravity_api.py`.
- The server logs `Antigravity quota summary is missing the gemini pool(s) for tier 'pro'` when
  that happens on a paid tier — and `… missing the argon pool(s) …` when only the Argon pool is
  missing (a 1.2 User-Agent serves Gemini but not Argon).
- Compare with agy itself: `agy -p "/usage" --output-format json` prints the same groups the
  CLI sees — if the CLI shows a pool Runway does not, the UA is behind.

### No token events / zero cost
- Events only appear after `agy` conversations: check `~/.gemini/antigravity-cli/conversations/` for `*.db` files.
- Cost is $0 for `unknown`/GPT-OSS model ids — this is expected.

## Related Files

| File | Purpose |
|---|---|
| `app/services/collectors/antigravity.py` | Server collector entry point |
| `app/services/collectors/antigravity_api.py` | `retrieveUserQuotaSummary` API mixin |
| `app/services/collectors/antigravity_oauth.py` | OAuth token cache-read mixin |
| `scripts/sidecar_pkg/event_extractors/antigravity.py` | Conversation DB parser |
| `scripts/sidecar_pkg/agy_version.py` | Installed agy CLI's version (reported with the token) |
| `scripts/sidecar_pkg/keyring_value.py` | Unwraps the go-keyring envelope the macOS Keychain entry stores |
| `scripts/sidecar_pkg/agy_keychain.py` | The macOS Keychain entry: catalogued rule, CLI version, and what the keep-alive reads |
| `scripts/sidecar.py` | Sidecar credential rule + event dispatch |
| `scripts/sidecar_pkg/keep_alive.py` | Optional `--keep-alive` thread (`agy models` renewal) |
| `scripts/reclassify_antigravity_models.py` | One-shot repair for pre-version-preservation rows |
| `scripts/backfill_argon_models.py` | One-shot repair for pre-Argon-family rows (rewrites the stored model id and re-prices) |
| `app/services/maintenance/argon_retag.py` | Shared logic behind the Argon repair script |
| `scripts/recost_events.py` | Reprice after pricing-seed changes (`--provider antigravity`) |
| `app/services/pricing_seed.py` | Antigravity pricing rows |
