# xAI (Grok)

**File:** `app/services/collectors/xai.py`

Credit balance and on-demand usage of the consumer Grok subscription through xAI's CLI chat proxy (server), plus per-turn usage events from the Grok CLI's session files (sidecar). The OpenAI-compatible `XAI_API_KEY` developer API is a separate product and is not used.

## At a glance

- **Strategy:** `api` only (OAuth bearer against the CLI chat proxy).
- **Cards:** one credits card, `weekly` or `monthly` according to the billing response's `currentPeriod.type` (anything unrecognised is `monthly`), plus an optional USD on-demand card when `onDemandCap` is non-zero. The tier badge comes from a best-effort `/v1/settings` call.
- **Labels:** `data_source: api`; `input_source` is `sidecar`, `config` (pasted bearer) or `server` (environment variable). See [architecture.md](../architecture.md#standard-definitions).

## Credentials

The collector needs an access bearer (`xai_access`). A refresh token (`xai_refresh`) alone does not configure it.

| Source | Where | Runs in | Notes |
|---|---|---|---|
| OpenCode | `auth.json`, `xai.access` and `xai.refresh` | sidecar | On OpenCode v2 the same fields come from the `credential` table in `opencode.db` ([opencode.md](opencode.md)). |
| Grok CLI | `~/.grok/auth.json` or `$GROK_HOME/auth.json`, an xAI OIDC scope entry: `key` is the access token, `refresh_token` the refresh token | sidecar | Account identity is the entry's email, then its user id; otherwise tag it in the usual flow. |
| Environment | `GROK_OAUTH_TOKEN` | sidecar or server | Access token only, no refresh. |
| Settings paste | Providers, xAI | server | Stored as `provider_configs.api_key` and mirrored into the token cache's `xai_access`. |

**Origins and tags.** Sidecar sources get key-scoped origins. Because xAI issues a new refresh token on every refresh and the access JWT lasts about six hours, the fingerprint is taken from a stable claim of the access JWT, not from either token. Details: [Credential origins and operator tags](../architecture.md#credential-origins-and-operator-tags).

**Who refreshes.** xAI rotates its refresh token, so the server never refreshes a login a sidecar pushed; that would sign the CLI out. The login renews when the CLI next runs, or automatically when the sidecar runs with `--keep-alive`, which refreshes shortly before expiry and writes the result back to the CLI's own file (or the OpenCode v2 database). Logins configured on the server (Settings paste, environment) are refreshed by the server when a refresh token is present. See [Who refreshes an OAuth login](../architecture.md#who-refreshes-an-oauth-login) and [keep-alive](../sidecar.md#keep-alive-for-antigravity-claude-code-codex-and-xai-opt-in).

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` | Bearer plus header `x-xai-token-auth: xai-grok-cli` | Credits and on-demand usage |
| `GET https://cli-chat-proxy.grok.com/v1/settings` | Same | Subscription tier (`subscription_tier_display`), best effort |

The collector checks the JWT expiry before calling. An expired token with no refresh token, or a 401/403, gives an `auth_failed` card.

## Usage events (sidecar)

- **Source:** `~/.grok/sessions/<url-encoded-cwd>/<session-id>/updates.jsonl` (under `$GROK_HOME` when set). `signals.json` is ignored: its `totalTokensBeforeCompaction` is a cumulative context size, not session usage.
- **Event id:** stable, from session, prompt and model. Replays overlap the watermark by one second so turns sharing a timestamp are kept; the server deduplicates on the id.
- **Model resolution:** with `modelUsage` present, one event per model; otherwise the turn totals and the latest model selection in the update stream.
- **Tokens and cost:** input, cache-read, cache-create, output and reasoning are split so cached input and reasoning are not counted twice. Reported cost is included only when usage is complete and not marked partial.
- **Bootstrap:** `SIDECAR_BOOTSTRAP_DAYS` (90 days by default).
- OpenCode events with `providerID: "xai"` are retagged onto provider `xai`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "xAI session expired, re-login with the Grok or OpenCode CLI" | Token expired with no usable refresh token, or xAI rejected it. Log in again with the CLI. |
| "xAI token expired; it renews when the CLI next runs" | The CLI owns the login. Run the CLI, or start the sidecar with `--keep-alive`. |
| "xAI quota response could not be parsed" | The billing payload changed shape; check the server log. |
| Card stays stale after a timeout or network error | No error card is raised for those; the next cycle retries. |
| A file-sourced login does not take the pasted-bearer tag | Check the origin shown in Fleet, Untagged Credentials and tag that. |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/xai.py` | Quota collector |
| `scripts/sidecar.py` | Credential dispatch, identity selection, session discovery |
| `scripts/sidecar_pkg/identity.py` | Origin fingerprinting (`jwt_stable_subject`) |
| `scripts/sidecar_pkg/xai_renewer.py` | Keep-alive renewer |
| `scripts/sidecar_pkg/event_extractors/xai.py` | Completed-turn event extraction |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | OpenCode events retagged onto xAI |
| `app/core/registry.json` (`providers.xai`) | Credential rules and UI help text |
