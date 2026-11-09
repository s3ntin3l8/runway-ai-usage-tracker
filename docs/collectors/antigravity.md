# Antigravity

**File:** `app/services/collectors/antigravity.py`

Quota gauges for the Antigravity CLI (`agy`) from Google's Cloud Code Assist API (server), and per-message token and cost events from the local conversation databases (sidecar).

## At a glance

- **Strategy:** `api` only.
- **Cards:** up to five, in three quota pools (`variant`):

| Pool | `variant` | Windows (`window_type`) |
|---|---|---|
| Gemini Models (Flash, Pro) | `gemini` | weekly, 5-hour (`session`) |
| Claude and GPT models | `frontier` | weekly, 5-hour (`session`) |
| Gemini 4 Argon (paid Pro and Ultra plans) | `argon` | weekly |

- **Labels:** `data_source: api`; `input_source` follows where the cached token came from (normally `sidecar`). See [architecture.md](../architecture.md#standard-definitions).
- Each card has `pct_used = round((1 - remainingFraction) * 100, 4)`, `reset_at` from `resetTime`, and `quota_pool_id = "antigravity:<variant>:<window>"`.

## Credentials

| Source | Where | Runs in | Notes |
|---|---|---|---|
| Token file | `~/.gemini/antigravity-cli/antigravity-oauth-token` (Linux, macOS with agy before 1.4.3) | sidecar | JSON: `auth_method`, `token.access_token`, `token.refresh_token`, `token.expiry`. |
| macOS Keychain | service `gemini`, account `antigravity` (agy 1.4.3 and later) | sidecar | Read through the Keychain worker after a one-time local consent; see [source-consent.md](../source-consent.md). Value is go-keyring wrapped (base64 or hex envelope), unwrapped by the sidecar. |
| Windows Credential Manager | target `gemini:antigravity` | sidecar | Native read. |

The server never reads a credential store. The sidecar ships the token on every push, under the origin `keychain:gemini:antigravity` for every OS-store source. A sidecar whose local token has lapsed does not push it, so one machine's dead session cannot overwrite the fresh entry another machine keeps. The macOS `id_token` is deliberately not mapped: the server derives the account label from an `id_token` when present, and a platform-specific mapping would file one account under two keys.

Run the sidecar on the host where `agy` is logged in; run `agy` once to create the credential. A server without a sidecar has no Antigravity credential (`make dev` alone does not see it; use `make dev-all`). On macOS with agy 1.4.3 or newer, enable the **Antigravity CLI** source in the local Sources UI and click **Authorize** once; nothing is read until then.

### Token lifetime & keep-alive

The access token lives one hour and only `agy` can renew it: the stored bundle has no OAuth client id, so neither the server nor the sidecar can refresh it (`REFRESHABLE = False`; a 401 just re-reads the cache in case another host pushed a live token). A host without regular agy sessions therefore lapses hourly. Options:

1. **Sidecar keep-alive (opt-in):** `--keep-alive` or `"keep_alive": true` in `config.json`. Each minute the sidecar checks the local token and, once it has lapsed, runs `agy models`, a metadata call that rewrites the access token and `expiry` in place without rotating the refresh token or calling a model. A failed attempt backs off to one retry every five minutes and never takes the sidecar down. On macOS the expiry is read from the Keychain entry, so it stays quiet until the source is consented. Details: [sidecar keep-alive](../sidecar.md#keep-alive-for-antigravity-claude-code-codex-and-xai-opt-in).
2. **A timer on hosts without the sidecar:** run `agy models` every few minutes (for example a systemd user timer with `OnUnitActiveSec=10min`). agy only rewrites the token once it has lapsed, so frequent runs are cheap.
3. **A live `agy` session** renews on each turn.

Keep-alive assumes `agy models` never rotates the refresh token. Before enabling it against a new agy release, diff the token's `refresh_token` across one renewal; a CLI that starts rotating it would sign every other machine out.

While the token is valid but within 10 minutes of expiry, the sidecar logs a pre-expiry warning (suppressed when keep-alive owns renewal).

**Multi-machine:** every sidecar pushes its own token under the resolved account and the server keeps the freshest. The collector discovers those bundles and fails over between them, so quota flows while any machine holds a live session. Fleet and Settings → Credentials show per-source state.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` with `{"metadata": {"ideType": "ANTIGRAVITY"}}` | Returns `cloudaicompanionProject` and the tier |
| `POST .../v1internal:retrieveUserQuotaSummary` with `{"project": "<id>"}` | Quota summary |

**User-Agent.** Google gates the endpoint on an agy-shaped User-Agent (`antigravity/cli/<version> (aidev_client; ...)`; without it the API answers 403), and the *version* selects which pools come back. Older versions get a subset with HTTP 200: below 1.2 only Claude and GPT, from 1.2 also Gemini, from 1.3 also Gemini 4 Argon. The version follows the installed CLI: the sidecar reports `agy --version` with the token (`scripts/sidecar_pkg/agy_version.py`, cached an hour on success and a minute on failure) and the collector builds the header from it. `_UA_FLOOR_VERSION` (currently 1.4.3) is used when no version was reported and is never undercut. The reported value is validated by `app/core/version.py` (ASCII digits, one to three parts) because it ends up in an HTTP header.

**Pool mapping.** Groups are matched by anchored name patterns that tolerate known cosmetic variants (trailing "Pool", "&" for "and", a version number on the Argon name). The Argon group is a separate pool and must not be folded into `gemini`, or the two would share one `latest_usage` slot and overwrite each other. An unknown group gets its own slug-named pool (`some-future-pool-<hash>`) plus a server-log warning, never an existing pool's slot. The collector returns a complete snapshot, so a pool that is no longer served is retired. The server logs `Antigravity quota summary is missing the <pool> pool(s) for tier '<tier>'` when a known tier lacks a pool it should have, which is the sign of a User-Agent that fell behind (debug level for free accounts).

## Usage events (sidecar)

- **Source:** `~/.gemini/antigravity-cli/conversations/<uuid>.db`, table `gen_metadata`; each row is one assistant turn holding a protobuf blob. The workspace path (`cwd`) comes from field 7 of `trajectory_metadata_blob`, a `file://` URI.
- **Event id:** `<conversation_id>|gen_<idx>`. There is no per-row timestamp: databases are filtered by file mtime against the watermark, and the mtime (spread by row index) stamps the events.

| Proto path | Meaning |
|---|---|
| `root.1.4.2` | Input tokens for the turn |
| `root.1.4.3` | Output tokens |
| `root.1.4.1` | Cache-read tokens |
| `root.1.4.5` | Cumulative total, not used |
| `root.1.19` | Raw model id; authoritative when present |
| `root.1.20` | KV metadata (`used_claude`, `used_claude_conservative`), consulted only when the raw id is empty |
| `root.1.21` | Display name, for example `Gemini 3.1 Pro (High)` |

- **Model resolution** (`normalize_ag_model` in `app/services/model_normalization.py`): the raw id wins. Claude slugs collapse to `claude-sonnet` (sonnet) or `claude-opus` (every other Claude, priced as opus). The whole `gemini-4-argon*` family becomes `gemini-4-argon`. Gemini flash and pro carry a minor version (`flash-3.5` to `flash-3.8`, `pro-3.1`) when it is seeded in pricing, otherwise clamp to `flash-3` / `pro-3`; flash-lite is `flash-lite-3`; a family with no 3.x signal stays bare (`flash`, `pro`, `flash-lite`). Other raw ids (for example `gpt-oss`) pass through verbatim. With an empty raw id the display name is used, and the KV flags only when that yields `unknown`.
- **Effort:** a trailing `(Low)`, `(Medium)` or `(High)` on the display name becomes `usage_events.effort`; `(Thinking)` or no suffix gives none.

## Pricing notes

Events use `provider_id="antigravity"` rows seeded in `app/services/pricing_seed.py`, independent of the `gemini` and `anthropic` rows. `claude-opus` and `claude-sonnet` use the official Claude API prices. Unpriced ids (`unknown`, GPT-OSS) cost $0, which is expected. After a normalization change, repair stored rows with `scripts/reclassify_antigravity_models.py` (server stopped), then `scripts/recost_events.py --provider antigravity`; `scripts/backfill_argon_models.py` rewrites older Argon rows to `gemini-4-argon` and reprices them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No quota cards | Run `agy models` to renew the token, then re-poll. A sidecar warning "local token expired ... not pushing" means this host's session lapsed and the server serves whichever machine still has a live token; use keep-alive or a live session. Settings → Credentials shows which sources are `auth_failed`. A 401 in the server log means every candidate token was dead (the server never refreshes Antigravity tokens). |
| Only the Claude/GPT pool, Gemini or Argon missing | The User-Agent version is behind the CLI. Check that the sidecar reports a version (sidecar log) and `_UA_FLOOR_VERSION` in `app/services/collectors/antigravity_api.py`. `agy -p "/usage" --output-format json` prints what the CLI sees. |
| No token events | Events appear only after `agy` conversations; check `~/.gemini/antigravity-cli/conversations/` for `*.db`. |
| Events cost $0 | Expected for `unknown` and GPT-OSS ids. |
| macOS: nothing is read | The source needs local consent (see Credentials). |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/antigravity.py` | Collector entry point |
| `app/services/collectors/antigravity_api.py` | Quota API mixin, User-Agent, pool mapping |
| `app/services/collectors/antigravity_oauth.py` | Token cache-read mixin |
| `app/services/collectors/oauth_base.py` | Shared OAuth token handling |
| `app/core/version.py` | Version validator |
| `app/services/model_normalization.py` | Model id normalization |
| `scripts/sidecar.py` | Credential rules and event dispatch |
| `scripts/sidecar_pkg/event_extractors/antigravity.py` | Conversation database parser |
| `scripts/sidecar_pkg/agy_version.py` | Installed CLI version, reported with the token |
| `scripts/sidecar_pkg/agy_keychain.py` | macOS Keychain entry |
| `scripts/sidecar_pkg/keyring_value.py` | go-keyring envelope unwrapping |
| `scripts/sidecar_pkg/keep_alive.py` | Keep-alive thread |
| `scripts/reclassify_antigravity_models.py` | Model id repair |
| `scripts/backfill_argon_models.py` | Argon row repair |
| `scripts/recost_events.py` | Reprice stored events |
