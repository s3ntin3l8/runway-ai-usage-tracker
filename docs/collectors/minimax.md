# MiniMax Collector

**File:** `app/services/collectors/minimax.py`

MiniMax coding plan quota collector.

## Overview

- **Collection Strategy**: api (REST) → web (Scraping Fallback)
- **Cards**: Multiple cards (one per model snapshot)
- **Authentication**: `MINIMAX_API_KEY` (api) OR `MINIMAX_COOKIE` (web)

## Setup Methods Quick Overview

The MiniMax collector uses a single API key for authentication:

1.  **API Key (MINIMAX_API_KEY)**:
    *   **Method**: Obtain your API key from the MiniMax platform and set it as an environment variable.
    *   **Details**: Refer to the [Configuration section](#configuration) for `MINIMAX_API_KEY` and [Troubleshooting: "Missing MINIMAX_API_KEY" error](#missing-minimax_api_key-error).

## Data Source

### Tier 1: api (Coding Plan API)
**Endpoint:** `https://api.minimax.io/v1/coding_plan/remains` (default — international host).
Set `MINIMAX_HOST=minimaxi.com` to switch to the China host (`https://api.minimaxi.com/v1/coding_plan/remains`).
**Auth:** Bearer token (api)

**Response:**
```json
{
  "model_remains": [
    {
      "model_name": "minimax-text-01",
      "remains": 500
    }
  ]
}
```
### Tier 2: web (HTML Scraping)
**URL:** `platform.minimax.io/user-center/payment/coding-plan`
**Auth:** Session cookie (web)
**Behavior:** Fallback used when the API token is missing or unauthorized.

## Output Format

```python
{
    "service": "MiniMax: minimax-text-01",
    "icon": "🤖",
    "remaining": "500",
    "unit": "requests",
    "reset": "Coding Plan",
    "health": "good",
    "pace": "Active",
    "detail": "minimax-text-01 quota [API]",
    "used_value": 0.0,
    "limit_value": 500.0,
    "unit_type": "count",
    "data_source": "api",
    "input_source": "config",
    "updated_at": "2026-04-11T16:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `MINIMAX_API_KEY` | Yes | MiniMax API key |
| `MINIMAX_HOST` | Optional | Override host. Default `minimax.io` (international); set to `minimaxi.com` for the China platform. |
| `MINIMAX_COOKIE` | Optional | Manual cookie header for the HTML scraping fallback when the API key is missing or unauthorized. |

## Sidecar Support

Sidecar uses the same API key. No aggregation needed. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "Missing MINIMAX_API_KEY" error
**Fix:**
1. Get key from https://api.minimax.io (international) or https://api.minimaxi.com (China)
2. `export MINIMAX_API_KEY="minimax-..."`
3. If you registered on the China platform, also set `MINIMAX_HOST=minimaxi.com`.

### API connection failed
**Cause:** Network error or invalid API key.
**Fix:** Verify your internet connection and API key validity.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/minimax.py` | Main collector |

## References

- **MiniMax (international):** https://www.minimax.io
- **MiniMax (China):** https://www.minimaxi.com
- **API Documentation:** https://api.minimax.io/document/main (or https://api.minimaxi.com/document/main for the China platform)

*Last updated: 2026-05-21*
