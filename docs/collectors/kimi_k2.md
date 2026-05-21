# Kimi K2 Collector (Credits)

**File:** `app/services/collectors/kimi_k2.py`

Kimi K2 (Credits) collector with credit balance tracking for the Kimi coding agent product.

## Overview

- **Collection Strategy**: REST API only
- **Cards**: 1 card (credit balance)
- **Authentication**: `KIMI_K2_API_KEY` environment variable

## Setup Methods Quick Overview

The Kimi K2 collector tracks prepaid credits specifically for the coding agent usage:

1.  **API Key (KIMI_K2_API_KEY)**:
    *   **Method**: Obtain your API key from the Kimi K2 platform and set it as an environment variable.
    *   **Details**: Refer to the [Configuration section](#configuration) for `KIMI_K2_API_KEY`.

## Data Source

### Primary: Kimi K2 Credits API
**Endpoint:** `https://kimi-k2.ai/api/user/credits`
**Auth:** Bearer token
**Key Format:** Minimum 10 characters

## Output Format

```python
{
    "service_name": "Kimi K2",
    "icon": "🌙",
    "remaining": "150.00",
    "unit": "credits",
    "reset": "Manual",
    "health": "good",
    "pace": "Stable",
    "detail": "Credits (consumed: 54.20)",
    "data_source": "api",
    "updated_at": "2026-04-19T06:51:51+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `KIMI_K2_API_KEY` | Yes | Kimi K2 API key (min 10 chars) |

## Troubleshooting

### "Missing/Invalid Key" error
**Fix:**
1. Ensure `KIMI_K2_API_KEY` is set in your environment.
2. The key must be at least 10 characters long.

### "Unauthorized"
**Fix:** Verify the API key is valid and has permissions to access the credits endpoint.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/kimi_k2.py` | Main collector for Kimi K2 credits |

## References

- **Kimi K2:** https://kimi-k2.ai
- **Kimi Coding IDE:** [Kimi Coding Collector](kimi_coding.md)
- **Moonshot API:** [Kimi API Collector](kimi_api.md)
