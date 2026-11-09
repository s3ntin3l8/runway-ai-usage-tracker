# Kimi Coding

**File:** `app/services/collectors/kimi_coding.py`

Quota for the Kimi For Coding subscription (5-hour, weekly and monthly windows). This is not the Moonshot Open Platform balance, which is a separate provider ([kimi_api.md](kimi_api.md), [kimi_k2.md](kimi_k2.md)).

## At a glance

- **Strategies** (both can stand alone; reorderable in Settings):
  1. `api`: `GET https://api.kimi.com/coding/v1/usages` with a Kimi Code API key or the Kimi Code CLI access token. Recommended.
  2. `web`: the kimi.com web gateway with the `kimi-auth` cookie. It also acts as the enrichment source: it sees windows the Code API omits.
- **Merge:** strategies run in their resolved order; the first success is the base and each later success enriches it (missing windows, the plan-title tier badge, ratio-only cards upgraded to counts). A failed strategy does not block a later one. See [collection_logic.md](../collection_logic.md).
- **Cards:** up to four: `session` (5h, request counts), `weekly`, and `monthly` with `variant` `total` or `code` (ratios). Inactive windows are suppressed, so a plan may show fewer (see Plan differences).
- **Labels:** `data_source` is `api` or `web`; `input_source` is `config`, `server` or `sidecar`.

## Credentials

Within the `api` strategy the order is: Settings key, `KIMI_CODE_API_KEY`, the token cache's `api_key` slot (a Settings paste mirrored there, or a key the sidecar discovered), then the Kimi Code CLI access token. The `web` strategy uses the Settings cookie, then `KIMI_AUTH_TOKEN`, then a sidecar-pushed browser cookie.

| Source | Where | Runs in | Notes |
|---|---|---|---|
| API key (recommended) | Settings, Kimi Coding, "API Key (Kimi Code Console)", or `KIMI_CODE_API_KEY` | server | Create at [kimi.com/code/console](https://www.kimi.com/code/console). Does not expire; works without a browser. |
| Kimi Code CLI | `~/.kimi-code/credentials/kimi-code*.json` | sidecar | The access token is reused read-only and skipped when `expires_at` is within 60 seconds of now; the refresh token is never used. The sidecar also forwards the CLI's `device_id` (next to `credentials/`), which the collector sends as `X-Msh-Device-Id`; if it is missing or invalid a deterministic per-account UUID is used instead. |
| OpenCode | `auth.json` entry `kimi-code-plan-global.key` | sidecar | Same key OpenCode uses for its own backend. Discovery: [opencode.md](opencode.md). |
| Cookie | `KIMI_AUTH_TOKEN` or the browser `kimi-auth` cookie | server or sidecar | Web JWT; expires. The web endpoints reject API keys. |

`KIMI_CODE_BASE_URL` overrides the Code API host for compatible proxies.

When Fleet pins a specific credential source, the collector uses that bundle's own credential type: a pinned CLI token stays a CLI token even if another source provides an API key.

**Origins.** The two key sources (`KIMI_CODE_API_KEY`, the OpenCode entry) get key-scoped origins, so a rotated key never inherits a tag ([Credential origins and operator tags](../architecture.md#credential-origins-and-operator-tags)). The CLI token and the cookie keep plain origins.

**OpenCode events.** Messages from OpenCode's `kimi-code-plan-global` backend are retagged onto this provider with their own account kept, so their token counts land on the same card when the OpenCode identity and this credential resolve to the same account. If the cards split, set the account label on this provider to match the OpenCode login email.

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET https://api.kimi.com/coding/v1/usages` | Bearer API key or CLI token | `limits[]` carries the 5h counts; `usages` carries ratio pools `limit_5h`, `limit_7d`, `limit_month_total`, `limit_month_code`. `booster_wallet` is ignored. |
| `GET https://api.kimi.com/coding/v1/me` | Same | Non-fatal identity call: `email` becomes the account label, `user_level_name` the tier badge, `goods_version` 2 marks a plan with no weekly window. |
| `POST .../BillingService/GetUsages` (kimi.com web gateway) | `kimi-auth` cookie | 5h and weekly counts |
| `POST .../MembershipService/GetSubscriptionStats` | Cookie | `ratelimitCode5h`, `ratelimitCode7d`, monthly total and code ratios |
| `POST .../MembershipService/GetSubscription` | Cookie | `goods.title` (tier badge) and goods version |

The `limit_5h` ratio can lag the real count, so the collector never prefers it over `limits[]`. The `user_id` in `/me` equals the `sub` claim of every Kimi JWT, one stable identity across credential types.

## Plan differences

- **Pro (goods version 2):** a 5h limit (100 requests) and a monthly credit pool. The web calls still report a vestigial weekly window and a `limit_month_code` pool that never accrues; both are hidden. Weekly is hidden whenever `GetSubscription` reports version 2. Monthly code is hidden when the total pool has usage but the code pool is exactly zero (when both are zero the card stays until usage disambiguates).
- **Legacy China plans (version 1):** report `user.membership.level` (`LEVEL_FREE`, `TRIAL`, `BASIC`, `INTERMEDIATE`, `ADVANCED`, shown as Adagio, Andante, Moderato, Allegretto, Allegro) and weekly request counts; the weekly card stays.
- With API-key-only auth the tier badge comes from `/me`; the web `GetSubscription` title is the fallback.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Invalid API Key (401)" | The Code API rejected the key. Create a fresh key at [kimi.com/code/console](https://www.kimi.com/code/console) and update the field. |
| "Kimi auth token invalid or expired" | The `kimi-auth` JWT expired. Re-login at [kimi.com/code](https://www.kimi.com/code) and refresh the cookie, or use an API key. |
| An API key pasted in the old "Auth Token (web)" field is rejected | That field is the cookie slot and the web gateway rejects keys. Move the key to the API key field. |
| No weekly card | Expected on version 2 plans such as Pro. |
| No tier badge | `/coding/v1/me` failed (it needs the same bearer as `/usages`), and no cookie is configured for the web fallback. |
| "Kimi Code CLI token expired" | Re-login with the CLI or set an API key. |
| "No Auth" | One of API key, CLI credential or cookie is required. |

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/kimi_coding.py` | Collector |
| `app/core/kimi_device.py` | Device id validation |
| `scripts/sidecar.py` | Credential rules, CLI token and device id discovery |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Retags `kimi-code-plan-global` events onto this provider |
| `scripts/reclassify_opencode_providers.py` | Re-applies the retag to already ingested events |
| `app/core/registry.json` (`providers.kimi_coding`) | Credential rules and UI help text |
