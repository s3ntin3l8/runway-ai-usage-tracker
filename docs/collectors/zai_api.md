# zAI API Collector (Balance)

**File:** `app/services/collectors/zai_api.py`

Zhipu AI (GLM) prepaid balance collector in Chinese Yuan (¥).

## Overview

- **Collection Strategy**: REST API only
- **Cards**: 1 card (account balance)
- **Authentication**: `ZAI_API_KEY` environment variable

## Setup Methods Quick Overview

The zAI API collector uses a single API key for authentication:

1.  **API Key (ZAI_API_KEY)**:
    *   **Method**: Obtain your API key from the Zhipu AI platform and set it as an environment variable.
    *   **Details**: Refer to the [Configuration section](#configuration) for `ZAI_API_KEY` and [Troubleshooting: "Missing/Invalid Key" error](#missinginvalid-key-error).

## Data Source

### Primary: Zhipu AI Balance API
**Endpoint:** `https://open.bigmodel.cn/api/paas/v4/users/me/balance`
**Auth:** Bearer token

**Response:**
```json
{
  "data": {
    "available_balance": "125.50"
  }
}
```

## Output Format

```python
{
    "service": "zAI (GLM)",
    "icon": "🌐",
    "remaining": "¥125.50",
    "unit": "balance",
    "reset": "Manual",
    "health": "good",
    "pace": "Stable",
    "detail": "Prepaid balance",
    "used_value": 0.0,
    "limit_value": 125.50,
    "is_unlimited": False,
    "unit_type": "currency",
    "currency": "CNY",
    "reset_at": None,
    "data_source": "api",
    "tier": None,
    "usage_url": "https://open.bigmodel.cn",
    "updated_at": "2026-04-07T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `ZAI_API_KEY` | Yes | Zhipu AI API key |

## Sidecar Support

Sidecar uses same API key. No aggregation needed (balance is account-wide). See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "Missing/Invalid Key" error
**Fix:**
1. Get key from https://open.bigmodel.cn/
2. `export ZAI_API_KEY="sk-..."`

### Shows ¥0.00 balance
**Cause:** No credits remaining
**Fix:** Add prepaid credits via Zhipu billing portal

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/zai_api.py` | Main collector |

## References

- **Zhipu AI:** https://open.bigmodel.cn
- **GLM Models:** ChatGLM series

> **Note:** For quota limits (tokens/time), see [zAI Plan Collector](zai_plan.md).
