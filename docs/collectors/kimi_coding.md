# Kimi Coding Collector (IDE)

**File:** `app/services/collectors/kimi_coding.py`

Kimi For Coding IDE quota collector with weekly and rate limit tracking.

## Overview

- **Collection Strategy**: web (Web API)
- **Cards**: 2 cards (Weekly quota, 5h rate limit)
- **Authentication**: `KIMI_AUTH_TOKEN` (web) OR Chrome `kimi-auth` cookie (web)

## Setup Methods Quick Overview

The Kimi Coding collector supports the following authentication methods:

1.  **JWT Token (KIMI_AUTH_TOKEN)**:
    *   **Method**: Obtain your JWT token (from browser DevTools) and set it as the `KIMI_AUTH_TOKEN` environment variable.
    *   **Details**: See [Configuration](#configuration) and [Troubleshooting: "No Auth" error](#no-auth-error).

2.  **Chrome `kimi-auth` Cookie**:
    *   **Method**: Log in to [kimi.com/code](https://www.kimi.com/code) in Chrome. Runway will automatically extract the `kimi-auth` cookie.
    *   **Details**: See [Troubleshooting: "No Auth" error](#no-auth-error).

## Data Source

### Primary: Kimi Coding API
**Endpoint:** `POST https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages`
**Auth:** JWT token (env var or Chrome cookie)

**Tiers:**
- Andante (¥49/mo): 1,024 req/week
- Moderato (¥99/mo): 2,048 req/week
- Allegretto (¥199/mo): 7,168 req/week

All tiers: 200 req per 5-hour rate limit window

## Output Format

```python
{
    "service": "Kimi Coding (Weekly)",
    "icon": "🌙",
    "remaining": "1834",
    "unit": "2048 req",
    "reset": "Weekly",
    "health": "good",
    "pace": "Moderato",
    "detail": "214 used · Moderato",
    "used_value": 214.0,
    "limit_value": 2048.0,
    "is_unlimited": False,
    "unit_type": "requests",
    "reset_at": "2026-01-09T15:23:13+00:00",
    "data_source": "web",
    "input_source": "manual",
    "tier": "Moderato",
    "usage_url": "https://www.kimi.com/code/console",
    "updated_at": "2026-04-07T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `KIMI_AUTH_TOKEN` | Optional* | JWT token from browser cookie |

*Either env var OR Chrome cookie required

## Sidecar Support

Sidecar extracts token from Chrome cookies. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "No Auth" error
**Fix:**
1. Login to https://www.kimi.com/code in Chrome
2. Or set `export KIMI_AUTH_TOKEN="eyJhbG..."` (copy from DevTools → Cookies)

### "401 Unauthorized"
**Cause:** Token expired
**Fix:** Re-login to https://www.kimi.com/code and extract fresh cookie

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/kimi_coding.py` | Main collector |
| `app/services/collectors/kimi_api.py` | API balance collector |
| `app/core/browser_cookies.py` | Cookie extraction |

## References

- **Kimi For Coding:** https://www.kimi.com/code

> **Note:** For API balance, see [Kimi API Collector](kimi_api.md).
