# Ollama Cloud

**File:** `app/services/collectors/ollama.py`

Server collector for the Ollama Cloud included-usage allowance, from an API key (preferred) or
a browser session cookie.

## At a glance

- **Strategies, in order** (`STRATEGIES`): `api` (`_get_ollama_api`) then `web`
  (`_get_ollama_web`). `api` returns nothing when no key is available or the data is
  unusable, and collection falls through to `web`.
- **Cards**
  - `api`, included credits: one `monthly` card, variant `allowance`, unit currency (USD
    spent of the allowance, with the refill timestamp).
  - `api`, legacy plan limits: one percent card per `session` / `weekly` entry.
  - `api`, pay-as-you-go with no allowance: one balance card, variant `credits`.
  - `web`: one card per usage meter. Window type comes from a concrete meter label (`Hourly
    usage` is `session`, `Weekly usage` is `weekly`), otherwise from the reset horizon; vague
    labels such as `Free usage` count only when no reset time is shown.
- **Labels:** `api` cards have `data_source: "api"`, `web` cards `"web"`. `input_source` is
  `config` (saved in Settings), `sidecar` (pushed) or `server` (env var). Values are defined in
  [Card provenance labels](../collection_logic.md#card-provenance-labels).
- The `allowance` and `credits` variants are separate pools because the card unit changed from
  percent to currency; `merge_card_json` freezes the quota fields of a stored card whose unit
  differs, and a separate pool lets the stale card be reconciled away.

## Credentials

Cookie precedence: saved account credentials, then sidecar values, then server env.

| Credential | Source | Runs in | Notes |
|---|---|---|---|
| API key | `OLLAMA_API_KEY` env, default account only | server | Used for `api` |
| API key | Settings (Providers, Ollama) | server | Per account |
| API key | opencode `auth.json` (`ollama-cloud.key`) in `~/.local/share/opencode/` or `~/.opencode/` | sidecar | Same key opencode bills against, so the card tracks CLI cloud usage |
| Session cookie | `OLLAMA_SESSION_TOKEN` env, default account only | server | Used for `web` |
| Session cookie | Browser cookie `session`, `ollama_session`, `__Host-ollama_session` or `__Secure-next-auth.session-token` on `ollama.com` | sidecar | Pushed as `session_cookie` |
| Session cookie | Settings | server | Bare value or full `Cookie` header |

The cookie is accepted as a bare value (sent as both `session` and `__Secure-session`) or a full
`Cookie` header. A value that looks like an API key (`sk-…`) in the cookie slot is rejected.

The opencode-discovered key has a key-scoped origin (`path:…/auth.json#<fingerprint>`); see
*Key-scoped origins* in [opencode.md](opencode.md). Pasting the same key in Settings makes the
server answer `provider:ollama#<fingerprint>`. Cookie candidates keep plain origins.

## Endpoints

| Strategy | Request | Notes |
|---|---|---|
| `api` | `GET https://ollama.com/api/balance`, `Authorization: Bearer <key>` | Quota source. `included.balance_usd`, `included.allowance_usd` and `included.period` give the allowance card; `included.session` / `included.weekly` (`remaining_percent`, `resets_at`) give legacy cards |
| `api` fallback | `GET https://ollama.com/api/usage` | Only on 404/405 from `/api/balance`, and only its legacy `limits.monthly.usage` shape; its spend report is ignored because spend comes from the CLI via the sidecar |
| `web` | `GET https://ollama.com/settings` | Parses each `data-usage-track` meter's `aria-label` (percent used) and nearby `data-time` (reset); legacy `Session usage` / `Weekly usage` blocks are still read. The plan tier comes from the `Included usage` (legacy `Cloud Usage`) badge |

A 401/403 from the balance call sets an "API key was rejected" error reason; an unparseable
body is treated as missing data. The cards link to `https://ollama.com/settings/billing`
(`api`) or `https://ollama.com/settings` (`web`).

## Usage events (sidecar)

Ollama usage driven through OpenCode is folded into these cards instead of appearing as a
separate `opencode-ollama` provider. In the OpenCode extractor, events with
`providerID: "ollama-cloud"` are retagged to `provider_id="ollama"` through the shared
canonical map (`scripts/sidecar_pkg/canonical_providers.py`) and their logged $0 cost is
dropped; the server reprices them from the seed. `app/services/pricing_seed.py` carries a
`provider_id="ollama"` row for every model on <https://ollama.com/pricing> (input / cached
input / output per 1M tokens), so a priced event equals what it took from the allowance.
Details are in [opencode.md](opencode.md). Notes:

- Tagged ids (`gemma4:31b`) resolve to the untagged row (`gemma4`); ids that carry their own
  tag (`gpt-oss:20b`, `gpt-oss:120b`) match their own rows.
- Only the standard (peak) rate is seeded. `deepseek-v4.1-flash` and `deepseek-v4-pro` are
  half price off-peak (outside 12:00-18:00 UTC on weekdays, and all weekend), so events in
  those hours are overcounted 2x until a time-of-day mechanism exists.
- `clef` publishes only an input rate; output and cache are seeded as 0.
- Models with no row (e.g. `web_search`, which has no token rate) stay at $0 and are listed
  by the Data Health unpriced-models check.
- Existing $0 Ollama events are re-costed when new rows are seeded (and by the Data Health
  fixer / `scripts/recost_events.py`); re-running is a no-op.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "API key detected" in the cookie field | An `sk-…` key was pasted where the session cookie belongs. Use the key field, or paste the cookie (below). |
| "API key was rejected" | The key is invalid or revoked; replace it in Settings, or remove it so the cookie path is used. |
| Card is empty or the `web` path fails | The cookie is expired or the page redirected to sign-in. Capture a fresh cookie (below). |
| Unexpected balance shape | Anything other than the shapes above is malformed quota data; collection falls back to `web`, which needs a cookie. |

Getting the cookie: sign in at `https://ollama.com/settings`, open DevTools, Network tab,
refresh, select the `settings` request and copy the `Cookie` request header (or just the
`__Secure-session=…` entry) into Settings. With a sidecar and a logged-in browser this is
automatic.

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/ollama.py` | Collector: `api` balance strategy and `web` scrape |
| `app/services/collectors/base.py` | Collector base class |
| `app/core/registry.json` | `ollama` credential rules (opencode file, env, cookies) |
| `scripts/sidecar_pkg/cookie_scan.py` | Cross-platform browser cookie store discovery |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Folds `ollama-cloud` OpenCode events onto `ollama` |
| `scripts/sidecar_pkg/canonical_providers.py` | Shared canonical provider map |
