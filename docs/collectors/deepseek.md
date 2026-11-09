# DeepSeek Collector

**File:** `app/services/collectors/deepseek.py`

DeepSeek prepaid (pay-as-you-go) balance collector.

## Overview

- **Strategy:** `api` (REST), `data_source: api`.
- **Cards:** one (Balance).

DeepSeek is a pure pay-by-use provider: there is no session/weekly/monthly
quota window upstream — the only gauge is the topped-up/granted balance.
Token usage and per-message costs reach Runway through the sidecar's opencode
event extractor, not through this collector.

## Credentials

`DEEPSEEK_API_KEY` in the environment or Settings, Providers (key from the [DeepSeek Platform](https://platform.deepseek.com/)). A sidecar also discovers the key from the environment or from OpenCode's `auth.json` entry `deepseek.key` ([discovery and origins](opencode.md)); a discovered key stays under Fleet, Untagged Credentials until an account is assigned, and only then does the balance collector see it. The balance itself is always fetched server-side.

## OpenCode usage events

DeepSeek through OpenCode comes from different billers that never share a card:

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

## Card

One `Balance` card (`window_type: rolling`, `unit_type: currency`, `data_source: api`): remaining total, with paid and granted balance in the detail. When `is_available` is false, `health` is at least `warning` and the detail notes the balance is unavailable for API calls.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `DEEPSEEK_API_KEY` | Yes* | DeepSeek API key (`sk-...`) |

\* Or a key saved in Settings, or discovered by the sidecar (see Credentials).

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
**Fix:** Label the account in Fleet → Untagged Credentials; the
`account_tag_hints` flow then retargets future events (same as OpenRouter).

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/deepseek.py` | Collector entry point — the prepaid balance endpoint |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Retags BYOK DeepSeek events onto `deepseek` |
| `scripts/sidecar.py` | Sidecar credential rules and event dispatch |
