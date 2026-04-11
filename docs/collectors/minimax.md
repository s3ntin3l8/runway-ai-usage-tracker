# MiniMax Collector

**File:** `app/services/collectors/minimax.py`

MiniMax coding plan quota collector.

## Overview

- **Collection Strategy**: REST API only
- **Cards**: Multiple cards (one per model snapshot)
- **Authentication**: `MINIMAX_API_KEY` environment variable

## Data Source

### Primary: MiniMax Coding Plan Remains API
**Endpoint:** `https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains`
**Auth:** Bearer token

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
    "updated_at": "2026-04-11T16:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `MINIMAX_API_KEY` | Yes | MiniMax API key |

## Sidecar Support

Sidecar uses the same API key. No aggregation needed. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "Missing MINIMAX_API_KEY" error
**Fix:**
1. Get key from https://api.minimaxi.com
2. `export MINIMAX_API_KEY="minimax-..."`

### API connection failed
**Cause:** Network error or invalid API key.
**Fix:** Verify your internet connection and API key validity.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/minimax.py` | Main collector |

## References

- **MiniMax:** https://www.minimaxi.com
- **API Documentation:** https://api.minimaxi.com/document/main

*Last updated: 2026-04-11*
