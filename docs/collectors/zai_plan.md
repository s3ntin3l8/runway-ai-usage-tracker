# zAI Plan Collector (Quota)

**File:** `app/services/collectors/zai_plan.py`

Zhipu AI (GLM) quota collector with token and time limit tracking.

## Overview

- **Collection Strategy**: api (Primary + Fallback)
- **Cards**: 1-2 cards (TOKENS_LIMIT, TIME_LIMIT)
- **Authentication**: `ZAI_API_KEY` (api)

## Setup Methods Quick Overview

The zAI Plan collector uses a single API key for authentication:

1.  **API Key (ZAI_API_KEY)**:
    *   **Method**: Obtain your API key from the Zhipu AI platform and set it as an environment variable. This is the same key used for the [zAI API Collector](zai_api.md).
    *   **Details**: Refer to the [Configuration section](#configuration) for `ZAI_API_KEY` and [Troubleshooting: "API Unavailable" error](#api-unavailable-error).

## Data Source

### Primary: Zhipu AI Quota API
**Primary:** `https://api.z.ai/api/monitor/usage/quota/limit`
**Fallback:** `https://open.bigmodel.cn/api/monitor/usage/quota/limit`
**Auth:** Bearer token

**Limit Types:**
- `TOKENS_LIMIT`: Token-based quota (e.g., 1M tokens)
- `TIME_LIMIT`: Time-based quota (e.g., 3600 minutes)

## Output Format

```python
{
    "service": "zAI Plan (Tokens)",
    "icon": "📊",
    "remaining": "550,000",
    "unit": "1,000,000 limit",
    "reset": "Plan cycle",
    "health": "warning",
    "pace": "Stable",
    "detail": "450,000 used · Basic Plan",
    "used_value": 450000.0,
    "limit_value": 1000000.0,
    "is_unlimited": False,
    "unit_type": "tokens",
    "reset_at": "2026-04-07T15:00:00+00:00",
    "data_source": "api",
    "input_source": "manual",
    "tier": "Basic Plan",
    "usage_url": "https://open.bigmodel.cn",
    "updated_at": "2026-04-07T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `ZAI_API_KEY` | Yes | Zhipu AI API key (same as zai_api) |

## Sidecar Support

Sidecar uses same API key. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "API Unavailable" error
**Fix:** Check both endpoints:
```bash
curl -H "Authorization: Bearer $ZAI_API_KEY" https://api.z.ai/api/monitor/usage/quota/limit
curl -H "Authorization: Bearer $ZAI_API_KEY" https://open.bigmodel.cn/api/monitor/usage/quota/limit
```

### "No Limits Found"
**Cause:** Plan has no configured limits (balance-only plan)
**Fix:** Verify plan includes quotas at https://open.bigmodel.cn/

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/zai_plan.py` | Main collector |
| `app/services/collectors/zai_api.py` | Balance collector |

## References

- **Zhipu AI:** https://open.bigmodel.cn

> **Note:** For prepaid balance, see [zAI API Collector](zai_api.md).
