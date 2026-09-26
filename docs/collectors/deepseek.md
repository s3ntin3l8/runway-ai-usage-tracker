# DeepSeek Collector

**File:** `app/services/collectors/deepseek.py`

DeepSeek prepaid (pay-as-you-go) balance collector.

## Overview

- **Collection Strategy**: api (REST)
- **Cards**: 1 card (Balance)
- **Authentication**: `DEEPSEEK_API_KEY` (api)

DeepSeek is a pure pay-by-use provider: there is no session/weekly/monthly
quota window upstream — the only gauge is the topped-up/granted balance.
Token usage and per-message costs reach Runway through the sidecar's opencode
event extractor, not through this collector.

## Setup Methods Quick Overview

1.  **API Key (`DEEPSEEK_API_KEY`)**:
    *   **Method**: Obtain your API key from the [DeepSeek Platform](https://platform.deepseek.com/) (API keys page) and set it as an environment variable or via the Runway UI settings.
    *   **Details**: Refer to the [Configuration section](#configuration) and [Troubleshooting: "Missing DEEPSEEK_API_KEY" error](#missing-deepseek_api_key-error).

## OpenCode CLI auto-discovery

If you have the [opencode CLI](opencode.md) installed with a BYOK DeepSeek
provider, the sidecar reads the key from
`~/.local/share/opencode/auth.json["deepseek"].key` (or
`~/.opencode/auth.json`) on every host that runs a sidecar, or from the
`DEEPSEEK_API_KEY` env var. That skips pasting the key, but it does not
attach the balance card by itself: the origin is key-scoped
(`path:…/auth.json#<fingerprint>`, see *Key-scoped origins* in
[opencode.md](opencode.md)), and the token stays blocked in **Fleet →
Untagged Credentials** until an account hint resolves. Only then does the
balance collector see the key.

### DeepSeek-direct vs. DeepSeek through the OpenCode Go subscription

Both flow through the opencode CLI, but they are **different billers** and
never share a card:

| How you use DeepSeek in opencode | opencode `providerID` | Runway `provider_id` | Billed to |
|----------------------------------|----------------------|----------------------|-----------|
| BYOK DeepSeek API key | `deepseek` | `deepseek` | Your DeepSeek prepaid balance (this collector) |
| DeepSeek models from the Go subscription | `opencode-go` | `opencode` | The OpenCode Go subscription |
| Free-tier deepseek models | `opencode` | `opencode-free` | Free tier |

BYOK events are retagged by `_OC_CANONICAL_MAP`
(`scripts/sidecar_pkg/event_extractors/opencode.py`) onto the canonical
`deepseek` provider so they land on the same account grain as the balance
card, with whatever cost OpenCode logged dropped so the server prices them from
`provider_pricing` (off-peak DeepSeek rates — see `app/services/pricing_seed.py`).
The Go tier is deliberately absent from that map: those tokens are already
paid for by the subscription and must never count against the DeepSeek
balance.

## Data Sources

### api (Balance API)
**Endpoint:** `https://api.deepseek.com/user/balance`
**Auth:** Bearer token (api)
**Timeout:** 10 seconds

**Response:**
```json
{
  "is_available": true,
  "balance_infos": [
    {
      "currency": "USD",
      "total_balance": "50.00",
      "granted_balance": "10.00",
      "topped_up_balance": "40.00"
    }
  ]
}
```

Returns account-level prepaid balance. When multiple currencies are returned,
the USD entry is preferred. `granted_balance` is promotional credit (spent
first by DeepSeek), `topped_up_balance` is real money.

There is no documented usage/token endpoint that accepts an API key —
detailed per-key usage lives behind the private Platform dashboard endpoints
which require a browser session. Token usage is therefore sourced from
opencode events.

## Output Format

### Card: Balance

```python
{
    "service_name": "DeepSeek",
    "variant": "Balance",
    "window_type": "rolling",
    "icon": "🐋",
    "remaining": "$50.00",
    "unit": "USD",
    "reset": "Prepaid",
    "health": "good",
    "pace": "Stable",
    "detail": "Paid: $40.00 / Granted: $10.00 [API]",
    "unit_type": "currency",
    "currency": "USD",
    "data_source": "api",
    "usage_url": "https://platform.deepseek.com/usage",
    "updated_at": "2026-09-26T12:00:00+00:00",
}
```

When `is_available` is false, `health` degrades to at least `warning` and the
detail gains `— balance unavailable for API calls`.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `DEEPSEEK_API_KEY` | Yes* | DeepSeek API key (`sk-...`) |

\* Or a key saved in the Runway dashboard (Settings → Providers), or read
from opencode's `auth.json` once an account hint resolves (Fleet →
Untagged Credentials).

## Sidecar Support

Sidecar discovers the key (env var / opencode `auth.json`) and, once an
account hint resolves, pushes it via the token cache; the balance itself
is fetched server-side. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "Missing DEEPSEEK_API_KEY" error
**Fix:**
1. Get a key from https://platform.deepseek.com (API keys)
2. `export DEEPSEEK_API_KEY="sk-..."` — or let the sidecar pick it up from opencode's `auth.json`, then label the account in Fleet → Untagged Credentials so the key is shipped

### API connection failed
**Cause:** Network error or invalid API key.
**Fix:** Verify your internet connection and API key validity.

### Events show under a different account than the balance card
**Cause:** The opencode account email and the balance card's account_id don't
match yet.
**Fix:** Label the account in Settings → Untagged Credentials; the
`account_tag_hints` flow then retargets future events (same as OpenRouter).

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/deepseek.py` | Main collector |
| `app/services/pricing_seed.py` | Off-peak DeepSeek rates for event costing |
| `app/core/config.py` | Settings (API key) |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | BYOK → `deepseek` event mapping |

## References

- **DeepSeek Platform:** https://platform.deepseek.com/
- **Models & Pricing:** https://api-docs.deepseek.com/quick_start/pricing
- **Balance endpoint contract:** `GET /user/balance` — not linked from the
  current docs nav; response shape cross-checked against CodexBar's
  [DeepSeek provider docs](https://github.com/steipete/CodexBar/blob/main/docs/deepseek.md)

*Last updated: 2026-09-26*
