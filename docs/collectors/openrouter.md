# OpenRouter Collector

**File:** `app/services/collectors/openrouter.py`

OpenRouter credit balance and per-key spending limit collector.

## Overview

- **Collection Strategy**: api (REST)
- **Cards**: 1 card (Credits balance) + 1 card (Key Limit, when configured)
- **Authentication**: `OPENROUTER_API_KEY` (api)

## Setup Methods Quick Overview

The OpenRouter collector uses a single API key for authentication:

1.  **API Key (OPENROUTER_API_KEY)**:
    *   **Method**: Obtain your API key from the OpenRouter dashboard and set it as an environment variable or via the Runway UI settings.
    *   **Details**: Refer to the [Configuration section](#configuration) for `OPENROUTER_API_KEY` and [Troubleshooting: "Missing OPENROUTER_API_KEY" error](#missing-openrouter_api_key-error).

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

## Output Format

### Card 1: Credits Balance

```python
{
    "service_name": "OpenRouter Credits",
    "icon": "🚀",
    "remaining": "$7.50",
    "unit": "USD",
    "reset": "Prepaid",
    "health": "good",
    "pace": "Stable",
    "detail": "Used: $2.50 of $10.00 [API]",
    "used_value": 2.5,
    "limit_value": 10.0,
    "unit_type": "currency",
    "data_source": "api",
    "updated_at": "2026-04-14T12:00:00+00:00"
}
```

### Card 2: Key Limit (only when per-key spending limit is configured)

```python
{
    "service_name": "OpenRouter Key Limit",
    "icon": "🔑",
    "remaining": "$19.50",
    "unit": "USD",
    "reset": "Per-key",
    "health": "good",
    "pace": "Stable",
    "detail": "Key used: $0.50 of $20.00 [API]",
    "used_value": 0.5,
    "limit_value": 20.0,
    "unit_type": "currency",
    "data_source": "api",
    "updated_at": "2026-04-14T12:00:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key |
| `OPENROUTER_HTTP_REFERER` | No | Sent as `HTTP-Referer` header (for OpenRouter attribution) |
| `OPENROUTER_X_TITLE` | No | Sent as `X-Title` header, defaults to `Runway` |

## Sidecar Support

Sidecar uses the same API key. No aggregation needed. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "Missing OPENROUTER_API_KEY" error
**Fix:**
1. Get key from https://openrouter.ai/keys
2. `export OPENROUTER_API_KEY="sk-or-v1-..."`

### API connection failed
**Cause:** Network error or invalid API key.
**Fix:** Verify your internet connection and API key validity.

### No Key Limit card shown
**Cause:** The `/api/v1/key` endpoint returned null/0 for `limit`, or the request timed out.
**Fix:** Set a per-key spending limit in your OpenRouter key settings.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/openrouter.py` | Main collector |
| `app/core/config.py` | Settings (API key, referer, title) |

## References

- **OpenRouter:** https://openrouter.ai
- **Credits API:** https://openrouter.ai/docs#credits
- **Key API:** https://openrouter.ai/docs#key

*Last updated: 2026-04-14*