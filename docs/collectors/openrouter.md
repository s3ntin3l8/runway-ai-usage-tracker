# OpenRouter Collector

**File:** `app/services/collectors/openrouter.py`

OpenRouter credit balance and per-key spending limit collector.

## Overview

- **Strategy:** `api` (REST), `data_source: api`.
- **Cards:** Credits, plus Key Limit when the key has a spending limit.

## Credentials

`OPENROUTER_API_KEY` in the environment or Settings, Providers. A sidecar also discovers the key from OpenCode's `auth.json` entry `openrouter.key` ([discovery and origins](opencode.md)); paste the same key in Settings and the server recognises it by fingerprint on the next sidecar cycle. OpenCode usage routed through OpenRouter is retagged onto `openrouter` (`_OC_CANONICAL_MAP` in `scripts/sidecar_pkg/event_extractors/opencode.py`) and lands on the same account.

## Data Sources

### Tier 1: api (Credits API)
**Endpoint:** `https://openrouter.ai/api/v1/credits`
**Auth:** Bearer token (api)
**Timeout:** 10 seconds

**Response:**
```json
{
  "data": {
    "total_credits": 10.00,
    "usage": 2.50
  }
}
```

Returns account-level credit balance: `remaining = total_credits - usage`.

### Tier 2: api (Key API - best-effort)
**Endpoint:** `https://openrouter.ai/api/v1/key`
**Auth:** Bearer token (api)
**Timeout:** 1 second (best-effort, non-blocking)

**Response:**
```json
{
  "data": {
    "label": "my-key",
    "limit": 20.00,
    "usage": 0.50,
    "rate_limit": { "requests": -1, "interval": "10s" },
    "is_free_tier": false
  }
}
```

When `limit` is present and > 0, a second card is produced showing per-key spending
usage. If the key has no spending limit configured (`limit` is null or 0), no key
card is emitted. If the endpoint fails or times out, the credits card is still
returned — this is a best-effort enrichment.

## Cards

- **Credits** (`variant: Credits`): remaining is `total_credits - usage`, prepaid, in USD.
- **Key Limit** (`variant: Key Limit`): only when `/key` returns a `limit` above zero; shows the key's spend against that limit.

Both are `unit_type: currency`, `data_source: api`.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key |
| `OPENROUTER_HTTP_REFERER` | No | Sent as `HTTP-Referer` header (for OpenRouter attribution) |
| `OPENROUTER_X_TITLE` | No | Sent as `X-Title` header, defaults to `Runway` |

## Troubleshooting

### Missing key
**Fix:**
1. Get key from https://openrouter.ai/keys
2. `export OPENROUTER_API_KEY="sk-or-v1-..."`

### API connection failed
**Cause:** Network error or invalid API key.
**Fix:** Verify your internet connection and API key validity.

### No Key Limit card shown
**Cause:** The `/api/v1/key` endpoint returned null/0 for `limit`, or the request timed out.
**Fix:** Set a per-key spending limit in your OpenRouter key settings.

## Related files

| File | Purpose |
|---|---|
| `app/services/collectors/openrouter.py` | Collector entry point |
| `scripts/sidecar.py` | Sidecar credential rules and event dispatch |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Retags OpenRouter events |
