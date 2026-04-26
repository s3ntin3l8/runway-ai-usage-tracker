# OpenCode Collector

**File:** `app/services/collectors/opencode.py`

OpenCode quota collector with web API (Chrome cookies) and local database fallback.

## Overview

- **Collection Strategy**: web (Web API) → local (SQLite DB)
- **Cards**: 3 cards (5h session, 7d weekly, 30d monthly windows)
- **Authentication:** Chrome session cookie (web) or local SQLite database (local)

## Setup Methods Quick Overview

The OpenCode collector supports the following authentication methods:

1.  **Manual Cookie (recommended for testing / Docker)**: Paste the `auth` cookie value from opencode.ai into the Runway settings panel.
    *   **Method**: See [Manual Cookie Setup](#manual-cookie-setup) below.

2.  **Chrome Session Cookie**: Automatically extracted from your Chrome browser — nothing to configure if you're already logged into opencode.ai in Chrome.
    *   **Details**: See [Troubleshooting: Web API returns empty](#web-api-returns-empty) if this isn't working.

3.  **Local SQLite Database**: Directly reads usage data from the OpenCode IDE's local database.
    *   **Method**: Use the OpenCode IDE on your machine at least once to create the DB.
    *   **Details**: See [Troubleshooting: Database not found](#database-not-found).

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

## Window Types

OpenCode tracks usage across three time windows with different reset behaviors:

| Window | Label | window_type | Reset Behavior |
|--------|-------|------------|---------------|
| 5h | "5h" | `session` | Rolling - resets ~5 hours from now |
| 7d | "7d" | `weekly` | Fixed - resets on fixed date (~4 days from now) |
| 30d | "Monthly" | `monthly` | Fixed - resets on fixed date (~10 days from now) |

The collector detects window type by examining `resetInSec` from the OpenCode API:
- `resetInSec > 86,400` (1 day) → **fixed** reset (7d, 30d)
- `resetInSec < 86,400` → **rolling** window (5h)

## Output Format

```python
{
    "service_name": "OpenCode (5h)",
    "icon": "⚡",
    "remaining": "$6.60",
    "unit": "$12 limit",
    "reset": "5h",
    "health": "good",
    "pace": "Stable",
    "detail": "$5.40 used (45.0%) · Web API | user@email.com",
    "used_value": 5.40,
    "limit_value": 12.0,
    "is_unlimited": False,
    "unit_type": "currency",
    "currency": "USD",
    "reset_at": "2026-04-26T19:43:00+00:00",  # Rolling window - resets in ~5 hours
    "window_type": "session",  # 5h=session, 7d=weekly, 30d=monthly
    "data_source": "web",
    "input_source": "config",
    "tier": "Go",
    "usage_url": "https://opencode.ai/workspace/{workspace_id}/go",
    "updated_at": "2026-04-26T14:30:00+00:00",
    # Token breakdown fields (when usage data available)
    "token_usage": {"input": 300, "output": 22014, "reasoning": 0, "cache_read": 6812194, "total": 22314},
    "by_model": {"qwen3.5-plus": {"cost": 0.23, "msgs": 50}},
    "msgs": 50,
    "pct_used": 1.0
}
```

## Token Breakdown Fields

When usage data is available from the OpenCode usage page, the collector enriches cards with structured token data:

| Field | Type | Description |
|-------|------|-------------|
| `token_usage` | dict | Token breakdown: `input`, `output`, `reasoning`, `cache_read`, `total` |
| `by_model` | dict | Per-model breakdown with `cost` and `msgs` |
| `msgs` | int | Total message count |
| `pct_used` | float | Percentage used based on cost vs limit |

This data enables token usage display in the UI and history graphs.

## Manual Cookie Setup

Use this method when browser cookie auto-extraction isn't available (Docker, Linux without Chrome, or just for testing).

### Step 1 — Get the cookie value

1. Open [opencode.ai](https://opencode.ai) in your browser and log in.
2. Open DevTools (`F12` or `Cmd+Option+I`).
3. Go to **Application** → **Storage** → **Cookies** → `https://opencode.ai`.
4. Find the cookie named **`auth`** and copy its **Value** column.

The value is a long opaque string. Do **not** include the cookie name — paste only the value itself (not `auth=<value>`, just `<value>`). Runway will also accept the full `auth=<value>` format and strip the prefix automatically.

### Step 2 — Paste it into Runway

1. Open the Runway dashboard → **SYS** → **Providers** → **opencode**.
2. In the **Auth Cookie (web)** field, click **Edit** and paste the value.
3. Click **Save**.

The next collection cycle will use this cookie to fetch your usage from the OpenCode web API.

### Notes

- The `auth` cookie typically expires after 30 days. If the web API cards stop appearing, refresh the cookie using the steps above.
- This method works in Docker (where browser extraction is unavailable) if you set the cookie via the dashboard or pass it via sidecar.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENCODE_GO_API_KEY` | Optional | API key for OpenCode Go billing (future integration) |
| `LOCAL_COLLECTOR_ENABLED` | Optional | Enable local DB fallback (default: true) |

## Sidecar Support

Sidecar queries local DB or extracts Chrome cookie. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### Web API returns empty
**Checklist:**
1. Are you logged into [opencode.ai](https://opencode.ai) in Chrome? Browser cookie extraction only works with Chrome/Chromium on the same machine.
2. Verify browser extraction: `python3 -c "from app.core.browser_cookies import get_opencode_session_cookie; print(get_opencode_session_cookie())"`
3. If that prints `None`, use [Manual Cookie Setup](#manual-cookie-setup) instead.

### Web API shows "public actor" error in logs
The `auth` cookie was sent to OpenCode but not recognised. Causes:
- Cookie has **expired** — get a fresh one from DevTools (see [Manual Cookie Setup](#manual-cookie-setup)).
- Cookie **value is wrong** — make sure you copied only the Value column, not the full `auth=<value>` string (Runway strips the prefix but double-check DevTools).

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
