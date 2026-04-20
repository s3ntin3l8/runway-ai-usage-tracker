# OpenCode Collector

**File:** `app/services/collectors/opencode.py`

OpenCode quota collector with web API (Chrome cookies) and local database fallback.

## Overview

- **Collection Strategy**: web (Web API) → local (SQLite DB)
- **Cards**: 3 cards (5h rolling, 7d rolling, 30d rolling windows)
- **Authentication:** Chrome session cookie (web) or local SQLite database (local)

## Setup Methods Quick Overview

The OpenCode collector supports the following authentication methods:

1.  **Chrome Session Cookie**: Automatically extracted from your Chrome browser.
    *   **Method**: Ensure you are logged into [opencode.ai](https://opencode.ai) in Chrome.
    *   **Details**: See [Troubleshooting: Web API returns empty](#web-api-returns-empty) for verification.

2.  **Local SQLite Database**: Directly reads usage data from the OpenCode IDE's local database.
    *   **Method**: Use the OpenCode IDE on your machine.
    *   **Details**: See [Troubleshooting: Database not found](#database-not-found) for ensuring the database exists.

## Data Sources

### Tier 1: web (Web API)
**Endpoints:**
- `opencode.ai/_server` (workspaces - get workspace ID)
- `opencode.ai/_server` (subscription - get usage data)

**Auth:** Chrome `session` cookie (web)
**Response:** JavaScript with regex-parsable usage data

### Secondary: Sidecar Aggregation
Aggregates data from multiple hosts via `opencode-<hostname>` providers.

### Tier 2: local (SQLite DB)
**Location:** `~/.local/share/opencode/opencode.db`
**Mechanism:** Directly reads the message database for cost snapshots.
**Windows:** 5h ($12), 7d ($30), 30d ($60) limits.

## Output Format

```python
{
    "service": "OpenCode (5h)",
    "icon": "⚡",
    "remaining": "$6.60",
    "unit": "$12 limit",
    "reset": "Rolling 5h",
    "health": "good",
    "pace": "Stable",
    "detail": "$5.40 used (45.0%) · Web API",
    "used_value": 5.40,
    "limit_value": 12.0,
    "is_unlimited": False,
    "unit_type": "currency",
    "currency": "USD",
    "reset_at": None,  # Rolling window - no fixed reset
    "data_source": "web",
    "input_source": "server",
    "tier": None,
    "usage_url": "https://opencode.ai/workspace/{workspace_id}/go",
    "updated_at": "2026-04-07T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENCODE_GO_API_KEY` | Optional | API key for OpenCode Go billing (future integration) |
| `LOCAL_COLLECTOR_ENABLED` | Optional | Enable local DB fallback (default: true) |

## Sidecar Support

Sidecar queries local DB or extracts Chrome cookie. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### Web API returns empty
**Fix:**
1. Login to https://opencode.ai in Chrome
2. Check cookie extraction: `python3 -c "from app.core.browser_cookies import get_opencode_session_cookie; print(get_opencode_session_cookie())"`

### Database not found
**Fix:** Use OpenCode IDE at least once to create `~/.local/share/opencode/opencode.db`

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/opencode.py` | Main collector |
| `app/core/browser_cookies.py` | Cookie extraction |
| `app/services/external_metrics.py` | Sidecar aggregation |

## References

- **OpenCode:** https://opencode.ai
