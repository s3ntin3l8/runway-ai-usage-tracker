# Kimi Coding Collector (Kimi For Coding)

**File:** `app/services/collectors/kimi_coding.py`

Kimi For Coding quota collector with 5-hour session, weekly, and monthly
window tracking. Tracks the subscription, not the Moonshot Open Platform
balance — those are separate providers (see *Related Files*).

## Overview

- **Strategies** (both standalone-capable, reorderable in the UI):
  1. **`api`** — `GET https://api.kimi.com/coding/v1/usages` with a Kimi Code
     API key or the Kimi Code CLI access token. *Recommended.*
  2. **`web`** — kimi.com web gateway (`GetUsages` + `GetSubscriptionStats` +
     `GetSubscription`) with the `kimi-auth` cookie. Also the **enrichment**
     source: it sees windows the Code API omits.
- **Cards**: up to 4 — 5h session (counts), weekly (ratio), monthly total
  (ratio), monthly code (ratio). **Inactive windows are suppressed**, so a
  plan may show fewer (see *Plan realities* below).
- **Merge semantics**: strategies run in their resolved (user-reorderable)
  order; the **first success is the base** and each later success enriches it
  — adding missing windows, the plan-title tier badge, and upgrading
  ratio-only cards to real counts. Reorder the strategies in Settings to flip
  which source is authoritative.

## Authentication

Priority within the `api` strategy: **DB API key (UI) → `KIMI_CODE_API_KEY`
env → Kimi Code CLI access token** (read-only, from
`~/.kimi-code/credentials/kimi-code.json`; skipped when `expires_at` is stale
— re-login with the CLI or set an API key). The `web` strategy uses the
`kimi-auth` cookie: DB session cookie → `KIMI_AUTH_TOKEN` env → sidecar-pushed
browser cookie.

| Method | Where | Notes |
|--------|-------|-------|
| **API key (recommended)** | Settings → Kimi Coding → "API Key (Kimi Code Console)", or `KIMI_CODE_API_KEY` | Create at [kimi.com/code/console](https://www.kimi.com/code/console). Never expires; works on hosts without a browser. |
| Kimi Code CLI | auto-discovered from `~/.kimi-code/credentials/kimi-code.json` | Read-only reuse of the CLI's access token; the refresh token is never used. |
| Cookie (legacy) | `KIMI_AUTH_TOKEN` env or browser `kimi-auth` cookie | Web JWT; expires. Only source that can see the weekly window + plan title on plans where the Code API omits them. |

> **Note:** OpenCode events served by its `kimi-code-plan-global` backend are
> retagged onto this provider (`_OC_CANONICAL_MAP` in
> `scripts/sidecar_pkg/event_extractors/opencode.py`) with their own account
> kept (pass-through), so their token counts enrich the same account's quota
> cards — set an account label on the Kimi Coding provider to match your
> OpenCode login email if the cards split.

## Endpoints

### Code API (api strategy)
**Endpoint:** `GET https://api.kimi.com/coding/v1/usages` (override host via
`KIMI_CODE_BASE_URL`)
**Auth:** `Authorization: Bearer <api key or CLI access token>`

Response (Pro plan, verified 2026-09-18): `limits[]` carries the 5h window
counts; `usages` carries ratio pools (`limit_5h`, `limit_7d`,
`limit_month_total`, `limit_month_code`); `booster_wallet` is ignored. The
`limit_5h` ratio demonstrably lags the counts (0% vs a real 47%) — the
collector never prefers it over `limits[]`.

### Web gateway (web strategy)
**Endpoints:**
- `POST .../BillingService/GetUsages` — 5h counts + weekly counts
- `POST .../MembershipService/GetSubscriptionStats` — `ratelimitCode5h`,
  `ratelimitCode7d`, `subscriptionBalance` (monthly total + code ratios)
- `POST .../MembershipService/GetSubscription` — `goods.title` → tier badge

**Auth:** `kimi-auth` JWT cookie (web endpoints reject API keys with 401).

## Plan realities (verified against Pro)

- **Pro** (¥/$39/mo, `GOODS_VERSION_V2`, goods title "Pro"): 5h session limit
  **100** and a monthly credit pool (ratios only). The web calls still report
  a **vestigial weekly window** (`ratelimitCode7d` / GetUsages weekly detail)
  and a **`limit_month_code` pool that never accrues** (coding draws from the
  total pool) — both are suppressed so Pro renders session + monthly total
  only:
  - *Weekly*: hidden when `GetSubscription` reports `GOODS_VERSION_V2` (V2
    plans are session + monthly; weekly is a legacy V1-tier quota). The Code
    API omits it entirely on V2.
  - *Monthly code*: hidden when the total pool has accrued usage (`>0`) but
    the code pool sits at exactly `0` — proof the code pool isn't the binding
    one. Both at `0` (fresh month) is ambiguous, so the card stays until
    usage disambiguates.
  The Code API returns **no** `usage`, `user.membership`, or `version` fields
  — the tier badge comes from the web strategy's `GetSubscription` enrichment.
  With API-key-only auth the tier badge is absent unless a cookie is also
  configured.
- Legacy China plans (`GOODS_VERSION_V1`) report `user.membership.level`
  (`LEVEL_FREE`/`TRIAL`/`BASIC`/`INTERMEDIATE`/`ADVANCED` → Adagio/Andante/
  Moderato/Allegretto/Allegro) and weekly request counts — the weekly card
  stays for these.

## Output Format

```python
{
    "service_name": "Kimi Coding",
    "window_type": "session",  # "session" (5h) | "weekly" | "monthly"
    "variant": None,  # monthly only: "total" | "code"
    "icon": "⏱️",
    "remaining": "59",  # counts cards: requests left; ratio cards: "98.9%"
    "unit": "100 req",  # ratio cards: "%"
    "reset": "in 3h",  # from reset_at
    "health": "good",
    "pace": "Stable",
    "detail": "41 used · 5h rate limit",
    "used_value": 41.0,  # None on ratio-only cards
    "limit_value": 100.0,  # None on ratio-only cards
    "pct_used": 41.0,
    "is_unlimited": False,
    "unit_type": "requests",  # "percent" on ratio-only cards
    "reset_at": "2026-09-18T18:52:05+00:00",
    "tier": "Pro",  # only when known (web enrichment or V1 membership)
    "data_source": "api",  # "api" | "web"
    "input_source": "config",  # "config" | "server" | "sidecar"
    "usage_url": "https://www.kimi.com/code/console",
    "updated_at": "2026-09-18T16:00:00+00:00",
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `KIMI_CODE_API_KEY` | Optional* | Kimi For Coding API key (api strategy) |
| `KIMI_CODE_BASE_URL` | Optional | Code API host override (compatible proxies) |
| `KIMI_AUTH_TOKEN` | Optional* | Legacy web JWT (web strategy) |

*One of API key / CLI credential / cookie is required.

## Migration (one-time)

If you pasted your API key into the old "Auth Token (web)" field, it sits in
the session_cookie slot (the web gateway rejects it). Move it:

```bash
RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \
    python scripts/migrate_kimi_coding_key.py --dry-run   # then --apply
```

Existing OpenCode kimi events (tagged `opencode-kimi-code-plan-global`) fold
onto this card via:

```bash
RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \
    python scripts/reclassify_opencode_providers.py --providers opencode-kimi-code-plan-global --dry-run
# then --apply, then:
RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \
    python scripts/recost_events.py --provider kimi_coding
RUNWAY_CONFIG_DIR=~/.config/runway APP_HOST=127.0.0.1 \
    python scripts/cleanup_opencode_kimi_card.py --apply
```

## Troubleshooting

### "Invalid API Key (401)"
**Cause:** The API key was rejected by the Code API.
**Fix:** Create a fresh key at [kimi.com/code/console](https://www.kimi.com/code/console)
and update the "API Key (Kimi Code Console)" field.

### "Kimi auth token invalid or expired"
**Cause:** The `kimi-auth` web JWT expired.
**Fix:** Re-login at [kimi.com/code](https://www.kimi.com/code) and refresh the
cookie (or set an API key — the API key does not expire).

### No tier badge / no weekly card
**No weekly card on a V2 plan (Pro) is expected** — V2 plans have no real
weekly quota; the collector suppresses the vestigial window the web API still
reports. **No tier badge with API-key-only auth is also expected** — the Code
API omits membership data; add a `kimi-auth` cookie (or sign in with the Kimi
Code CLI plus browser session) so the web strategy can enrich the tier badge.
On V1-tier plans (cookie auth) the weekly card is the primary quota and stays.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/kimi_coding.py` | Main collector |
| `app/services/collectors/kimi_api.py` | Moonshot Open Platform balance |
| `app/services/collectors/kimi_k2.py` | Kimi K2 (kimi-k2.ai) credits |
| `scripts/migrate_kimi_coding_key.py` | Move misfiled API key to the right slot |
| `scripts/cleanup_opencode_kimi_card.py` | Remove stale opencode-derived card |

## References

- **Kimi For Coding:** https://www.kimi.com/code
- **API key console:** https://www.kimi.com/code/console
- **Kimi API pricing (notional cost seed):** https://platform.kimi.ai/docs/pricing/chat
