# Kimi API Collector (Balance)

**File:** `app/services/collectors/kimi_api.py`

Moonshot AI (Kimi) prepaid balance collector in USD ($).

## Overview

- **Collection Strategy**: api (Moonshot API)
- **Cards**: 1 card (account balance)
- **Authentication**: `KIMI_API_KEY` (api)

## Setup Methods Quick Overview

The Kimi API collector uses a single API key for authentication:

1.  **API Key (KIMI_API_KEY)**:
    *   **Method**: Obtain your API key from the Moonshot AI platform and set it as an environment variable.
    *   **Details**: Refer to the [Configuration section](#configuration) for `KIMI_API_KEY` and [Troubleshooting: "Missing/Invalid Key" error](#missinginvalid-key-error).

## Data Source

### Primary: Moonshot AI Balance API
**Endpoint:** `https://api.moonshot.ai/v1/users/me/balance`
**Auth:** Bearer token
**Key Format:** `sk-proj-...` (minimum 10 characters)

## Output Format

```python
{
    "service": "Kimi API",
    "icon": "🌙",
    "remaining": "$45.75",
    "unit": "balance",
    "reset": "Manual",
    "health": "good",
    "pace": "Stable",
    "detail": "Prepaid balance (API)",
    "used_value": 0.0,
    "limit_value": 45.75,
    "is_unlimited": False,
    "unit_type": "currency",
    "currency": "USD",
    "reset_at": None,
    "data_source": "api",
    "input_source": "config",
    "tier": None,
    "usage_url": "https://platform.moonshot.cn",
    "updated_at": "2026-04-07T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `KIMI_API_KEY` | Yes | Moonshot API key (min 10 chars) |

## Sidecar Support

Sidecar uses same API key. No aggregation needed (balance is account-wide). See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "Missing/Invalid Key" error
**Fix:**
1. Get key from https://platform.moonshot.cn/
2. `export KIMI_API_KEY="sk-proj-..."` (must be ≥10 chars)

### "401 Unauthorized"
**Fix:** Check key at https://platform.moonshot.cn/ and regenerate if needed

### Shows $0.00 balance
**Cause:** No credits remaining
**Fix:** Add prepaid credits via Moonshot billing portal

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/kimi_api.py` | Main collector |

## References

- **Moonshot AI:** https://platform.moonshot.cn
- **Kimi Models:** K2.5 series (2M token context)

> **Note:** For IDE coding quotas, see [Kimi Coding Collector](kimi_coding.md).
