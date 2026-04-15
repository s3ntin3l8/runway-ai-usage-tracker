# Antigravity Collector

**File:** `app/services/collectors/antigravity.py`

File-based quota collector for the Antigravity IDE.

## Overview

- **Collection Strategy**: Local JSON file only (no API calls)
- **Cards**: 1-3 cards (one per model)
- **Authentication**: None (reads local JSON file)

## Setup Methods Quick Overview

The Antigravity collector operates by reading a local JSON quota file. No API keys or external authentication are required.

1.  **Local JSON Quota File**:
    *   **Method**: Use the Antigravity IDE. The IDE automatically creates and updates a `quota.json` file on your local machine.
    *   **Configuration**: The path to this file is auto-discovered, but can be overridden using the `ANTIGRAVITY_QUOTA_PATH` environment variable.
    *   **Details**: See [Primary: Local JSON Quota File](#primary-local-json-quota-file) and [Configuration section](#configuration).

## Data Source

### Primary: Local JSON Quota File
**Location:** Platform-specific (configurable via `ANTIGRAVITY_QUOTA_PATH`):
- **Linux:** `~/.local/share/antigravity/state/quota.json`
- **macOS:** `~/Library/Application Support/antigravity/state/quota.json`
- **Windows:** `%APPDATA%\antigravity\state\quota.json`
**Format:**
```json
{
  "models": {
    "claude-sonnet-4": {
      "remaining_percent": 75.5,
      "resets_at": 1775570736
    }
  }
}
```

**Behavior:** Silent failure (returns empty list) if file missing - IDE is optional

## Output Format

```python
{
    "service": "AG: claude-sonnet-4",
    "icon": "🛸",
    "remaining": "75.5%",
    "unit": "remaining",
    "reset": "in 2h 15m",
    "health": "good",
    "pace": "Stable",
    "detail": "claude-sonnet-4 [IDE]",
    "used_value": 24.5,
    "limit_value": 100.0,
    "is_unlimited": False,
    "unit_type": "percent",
    "reset_at": "2026-04-07T15:00:00+00:00",
    "data_source": "local",
    "tier": None,
    "usage_url": None,
    "updated_at": "2026-04-07T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTIGRAVITY_QUOTA_PATH` | Optional | Path to quota JSON file (auto-discovered on Linux/macOS/Windows) |

## Sidecar Support

Sidecar can read file and forward contents. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### No Antigravity cards in dashboard
**Cause:** File not found or IDE not running
**Fix:**
1. Check file exists:
   - **Linux:** `ls ~/.local/share/antigravity/state/quota.json`
   - **macOS:** `ls ~/Library/Application\ Support/antigravity/state/quota.json`
2. Open Antigravity IDE to trigger quota write
3. Verify permissions: `chmod 644 <path-to-quota.json>`

### Shows 0% for all models
**Cause:** Quota file has zero values
**Fix:** Use models in IDE to generate usage data

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/antigravity.py` | Main collector |

## References

- **Antigravity IDE:** https://antigravity.ai

> **Note:** File updated when user checks quota in IDE or at IDE startup.
