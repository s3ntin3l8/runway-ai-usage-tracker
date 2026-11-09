# Claude

**File:** `app/services/collectors/anthropic.py` (provider `anthropic`) and `app/services/collectors/anthropic_console.py` (provider `anthropic_console`)

Claude subscription quotas (Pro/Max, via OAuth or a claude.ai session), Claude Console prepaid credit, and the Claude Code / Cowork usage events the sidecar extracts. The server reads no credential files; every local login arrives through the sidecar.

## At a glance

**`anthropic`** (display name Claude Pro). `STRATEGIES` in `anthropic.py`, in order:

| Strategy | Label | `data_source` | Endpoint |
|---|---|---|---|
| `oauth` | OAuth API | `api` | `api.anthropic.com/api/oauth/usage` |
| `web` | Web API | `web` | `claude.ai/api/organizations/{orgId}/usage` |

Cards: `Session` (5h, `session`), `Weekly` (7d, `weekly`), per-model weekly windows the API reports (for example `Opus Weekly`), `OAuth Apps`, and `Extra Usage`; the web strategy can add a prepaid `Balance` card. `input_source` is `sidecar` when the credential was pushed by a sidecar and `config` when it came from `CLAUDE_CODE_OAUTH_TOKEN` or Settings.

**`anthropic_console`** (display name Claude Console). One strategy, `web`, one `Balance` card (`window_type: rolling`, `data_source=web`, `input_source=config`), polled every 15 minutes.

The sidecar adds `data_source=local`, `input_source=sidecar` data: the statusline file (below) and per-message events. See [Strategy Types](../collection_logic.md#strategy-types) for how primary and enrichment data merge.

## Credentials

| Source | Where it comes from | Runs in | Notes |
|---|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Environment variable | server and sidecar | Optional; the sidecar reports it as an OAuth source too. |
| `~/.claude/.credentials.json` | Claude Code login, paired with `~/.claude.json` for `oauthAccount` identity | sidecar | Also `<Claude config dir>/oauth_creds.json` and `CLAUDE_CONFIG_DIR`. Defined in `app/core/registry.json`. |
| macOS Keychain `Claude Code-credentials` | Claude Code login on macOS | sidecar | Includes configured-directory service variants; the file above is only a fallback there. |
| Claude Desktop OAuth cache | `config.json` under Claude Desktop's Application Support directory (`oauth:tokenCacheV2`, legacy `oauth:tokenCache`) | sidecar, macOS | Forwards access tokens and expiry, never refresh tokens. See [macOS](#macos). |
| `sessionKey` cookie for `claude.ai` | Browser cookie stores, or Claude Desktop's cookie store on macOS | sidecar | Automatic discovery; the web strategy uses it when OAuth is unavailable. |
| Manual cookie bundle | Settings → Providers | server | Paste the full Cookie header of a `claude.ai/settings/usage` request (see below). |
| `sessionKey` cookie for `platform.claude.com` | Browser cookie stores, or pasted in Settings (Claude, Console API credits) | sidecar / server | Console only. A bare value or a full Cookie header is accepted. |
| `~/.claude/statusline.json` | Claude Code statusline | sidecar | Used only when modified in the last 5 minutes; emits Session and Weekly cards tagged `local`. |

A login on a machine's own `~/.claude` is never refreshed by the server (Anthropic's refresh tokens are single-use). With the sidecar's `--keep-alive` the sidecar renews it and writes it back; see [Keep-alive](../sidecar.md#keep-alive-for-antigravity-claude-code-codex-and-xai-opt-in). A login the server holds itself (Settings or env) is refreshed by the server.

When a discovered email matches an enabled Claude account, the origin is attached to it. Otherwise the source shows in Fleet for assignment, and in Settings → Credentials as unassigned. A manually assigned origin wins over the discovered email.

**Manual cookie bundle.** Log in to claude.ai, open DevTools, Network, load `claude.ai/settings/usage`, and copy the whole `Cookie` request header (or Copy as cURL). Paste it into the account's **Session cookie** field under Settings → Providers. A bare `sessionKey` works, but the full bundle is recommended so Cloudflare accepts the request; Runway mimics the browser's User-Agent and Client Hints.

## Endpoints

| Endpoint | Used by | Notes |
|---|---|---|
| `GET api.anthropic.com/api/oauth/usage` | `oauth` | Bearer token. |
| `GET api.anthropic.com/v1/organizations/me` | `oauth` | Identity fallback when the token holder is unknown. |
| `POST platform.claude.com/v1/oauth/token` | server-held logins | Refresh, only for logins the server owns. |
| `GET claude.ai/api/organizations` | `web` | Resolves the organization UUID. |
| `GET claude.ai/api/organizations/{orgId}/usage` | `web` | Quota windows. |
| `GET platform.claude.com/api/organizations/{orgId}/prepaid/credits` | `anthropic_console` | Private endpoint; the schema can change. |

## Claude Console

Console credit is a separate connection (Claude, Console API credits). The quota cookie cannot read Console billing and the Console cookie cannot read subscription quotas, and API keys do not work for the balance endpoint, so keep each credential in its own connection. Console cookies are stored with the normal credential encryption and must be replaced when the session expires. With one accessible organization it is selected automatically; add another connection to track another organization.

The card shows the full reported balance. Purchased credits and unexpired promotional grants explain the total; subscription credits carry `program_id=subscription_api_credit`. Grant expiry is shown as expiry. Credits can have product restrictions, shown in the connection overview. No token events or API spend are inferred from balance movements. A zero balance is valid. Auth, permission, transport and parse failures keep last-good data under the normal staleness policy and do not affect subscription quotas.

**Naming and billing.** An automatically discovered account is named after what it is: a subscription by the member's email, a Console account by the organization name (shared by all members). A plan publishes percentage quotas and bills as a subscription; a Console account publishes a prepaid balance and bills as pay-as-you-go. A claude.ai prepaid balance proves neither. The model is detected when the account is first recorded; a billing type nobody set is filled in on a later poll, and one set in Settings is never overwritten. For hand-added accounts, **Detect billing model** in the wizard only suggests a model; the operator's pick is saved. The identity a card proved (an organization-scoped one is marked `org: `) lives in the card's detail.

## Usage events (sidecar)

Source: `~/.claude/projects/**/*.jsonl` and `~/.config/claude/projects/**/*.jsonl` (plus `CLAUDE_CONFIG_DIR`), parsed by `scripts/sidecar_pkg/event_extractors/anthropic.py`. Only `assistant` records are read. Event id is `<message id>|<request id>` (message id alone without a request id), deduplicated on that pair. Model ids are normalized by `normalize_anthropic_model` to `family-version` (`opus-4.7`, `sonnet-3.5`); date snapshots are dropped and pricing falls back to the bare family. Conversation text is not uploaded.

Per-message columns on `usage_events`, defined in `app/models/db_usage.py` and added by the `ALTER TABLE` list in `app/core/db.py`:

| Column | Source field |
|---|---|
| `effort` | record `effort` (`low` to `max`) |
| `speed` | `usage.speed` (`fast` when fast mode was active) |
| `service_tier` | `usage.service_tier` (`standard`, `priority`, `batch`) |
| `entrypoint`, `app_version` | record `entrypoint` (`cli`, `sdk-ts`, `claude-vscode`, ...) and `version` |
| `tokens_cache_create_1h`, `tokens_cache_create_5m` | `usage.cache_creation.ephemeral_*_input_tokens`; `tokens_cache_create` stays the total |
| `web_search_requests`, `web_fetch_requests` | `usage.server_tool_use` |

Sidechain records also carry the subagent type. Claude Code sessions that run on Anthropic's cloud runner leave no local transcript; a server-side collector reads them instead, see [Claude cloud-runner sessions](../collection_logic.md#claude-cloud-runner-sessions).

## Pricing notes

The 1h cache-write portion bills at `cache_create_1h_per_mtok` (falling back to the 5m rate when none is seeded) instead of the 5m rate; this is why the TTL split is stored. See `app/services/cost_calculator.py`.

## macOS

- **Claude Code** usage comes from its project JSONL logs. Claude Desktop chat activity does not necessarily appear there, and ordinary Desktop Chat token usage is not collected.
- **Desktop cookie.** The sidecar reads the `sessionKey` for `claude.ai` from Desktop's `Cookies` store, using the `Claude Safe Storage` Keychain item, and forwards it through the normal credential pipeline. Desktop is tried before browser stores. Keychain results are cached in memory, a denied or timed-out lookup is not repeated until the sidecar restarts, and nothing is written to Desktop's login.
- **Desktop OAuth.** Described in Credentials. The server verifies the holder profile and readable quota before activating a discovered native OAuth source; OAuth sources are preferred over cookies at equal priority. Desktop and the CLI own renewal.
- **Browser sessions.** Chrome-family and Safari sessions for `claude.ai` and `platform.claude.com` are discovered separately and verified with their own APIs. Multiple eligible Console organizations appear in the pending-credentials dialog. An inaccessible store is never treated as logout; last-good sources are kept.
- **Quotas are not token events.** Desktop's `plan-usage-history.json` samples quota utilization, not tokens, and is not turned into events.
- **Cowork.** Session manifests under `~/Library/Application Support/Claude/local-agent-mode-sessions/` point to embedded `.claude/projects/**/*.jsonl` transcripts, read with the same parser and event ids as Claude Code (`scripts/sidecar_pkg/cowork_events.py`). All available history is eligible, independent of the CLI watermark. Per-transcript offsets and a prefix fingerprint live in `cowork-progress.json` beside the sidecar config; replays deduplicate on the server. Desktop Code normally writes ordinary project logs and is collected by the standard path.
- **Tray counters.** The tray separates the last successful server sync (including idle heartbeats) from the last successful data upload; metric and event counts belong to that upload, not to the process lifetime.
- **Opt out of Desktop access.** Set `RUNWAY_CLAUDE_DESKTOP_CREDENTIALS` to `0`, `false`, `off` or `no` in the sidecar environment to skip both Desktop OAuth and the Desktop cookie store (default on). Browser discovery and event collection continue. For a launchd-managed sidecar set it in `EnvironmentVariables`; a shell export does not reach a separately launched tray app.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No data", all sources exhausted | Log in with `claude login` and run the sidecar on that machine, set `CLAUDE_CODE_OAUTH_TOKEN`, or log in to claude.ai in a browser the sidecar can read. |
| 401 Unauthorized | Start the sidecar with `--keep-alive` (or enable it in Fleet) so the login renews before it lapses; otherwise it renews the next time Claude Code runs. After `invalid_grant`, run `claude auth login`. |
| Credential found but not attached | Its email matches no enabled account; assign it in Fleet. |
| Browser cookie decryption fails on macOS | Grant Keychain access for the browser's Safe Storage item, then restart the sidecar. |
| Console balance missing | Re-paste the `platform.claude.com` `sessionKey`; the claude.ai cookie does not work. |
| `make dev` shows no local login | The server reads no credential files; use `make dev-all`. |
| Sidecar cannot sync behind a proxy | The proxy must pass `/api/v1/fleet/config`, `/api/v1/fleet/credentials/manifest` and `/api/v1/fleet/ingest` ([example](../forward-auth.md#5-production-hardening-split-the-traefik-router)). |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/anthropic.py` | Collector entry point and strategy table |
| `app/services/collectors/anthropic_oauth.py` | OAuth bearer strategy and refresh |
| `app/services/collectors/anthropic_web.py` | claude.ai web session strategy |
| `app/services/collectors/anthropic_console.py` | Console prepaid balance |
| `app/services/collectors/anthropic_common.py` | Shared window names and card shaping |
| `app/services/collectors/oauth_base.py` | Shared OAuth token handling |
| `app/services/claude_cloud_collector.py` | Cloud-runner session collector |
| `scripts/sidecar.py` | Credential rules, statusline rule, event dispatch |
| `scripts/sidecar_pkg/event_extractors/anthropic.py` | Claude Code usage events |
| `scripts/sidecar_pkg/cowork_events.py` | Cowork transcript events |
| `scripts/sidecar_pkg/claude_code_keychain.py` | Claude Code Keychain read |
| `scripts/sidecar_pkg/claude_desktop.py` | Claude Desktop OAuth and cookie read |
| `scripts/sidecar_pkg/cookie_scan.py` | Browser cookie store discovery |
| `scripts/sidecar_pkg/macos_cookies.py` | macOS Chrome cookie decryption |
| `scripts/sidecar_pkg/safari_cookies.py` | Safari cookie decryption |
| `scripts/sidecar_pkg/anthropic_renewer.py` | Keep-alive renewer |
| `scripts/sidecar_pkg/claude_keychain_renewer.py` | Keychain write-back renewer |
| `scripts/sidecar_pkg/claude_renewal_lock.py` | Per-item renewal locking |
| `app/models/db_usage.py` | Per-message event columns |
