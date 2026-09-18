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
  (ratio), monthly code (ratio).
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
> `scripts/sidecar_pkg/event_extractors/opencode.py`), so their token counts
> enrich these same cards.

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
  **100**, weekly limit **100** (weekly visible only via web), monthly credit
  pool (ratios only). The Code API returns **no** `usage`, `user.membership`,
  or `version` fields — the tier badge and weekly card come from the web
  strategy's enrichment. With API-key-only auth those two are absent.
- Legacy China plans (`GOODS_VERSION_V1`) report `user.membership.level`
  (`LEVEL_FREE`/`TRIAL`/`BASIC`/`INTERMEDIATE`/`ADVANCED` → Adagio/Andante/
  Moderato/Allegretto/Allegro) and weekly request counts.

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
**Expected with API-key-only auth.** The Code API omits membership and weekly
data; add a `kimi-auth` cookie (or sign in with the Kimi Code CLI plus browser
session) so the web strategy can enrich.

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
