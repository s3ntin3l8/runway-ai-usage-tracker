# OpenRouter Collector

**File:** `app/services/collectors/openrouter.py`

OpenRouter credit balance collector.

## Overview

- **Collection Strategy**: REST API only
- **Cards**: 1 card (Credits balance)
- **Authentication**: `OPENROUTER_API_KEY` environment variable

## Data Source

### Primary: OpenRouter Credits API
**Endpoint:** `https://openrouter.ai/api/v1/credits`
**Auth:** Bearer token

**Response:**
```json
{
  "data": {
    "total_credits": 10.00,
    "usage": 2.50
  }
}
```

## Output Format

```python
{
    "service": "OpenRouter Credits",
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
    "updated_at": "2026-04-11T16:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key |

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

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/openrouter.py` | Main collector |

## References

- **OpenRouter:** https://openrouter.ai
- **Credits API:** https://openrouter.ai/docs#credits

*Last updated: 2026-04-11*
