# Gemini Collector

**File:** `app/services/collectors/gemini.py`

Google Gemini CLI quota collector with OAuth-backed API and local log fallback.

## Overview

- **Collection Strategy**: OAuth API (with auto-refresh) → Local session logs
- **Cards**: 1-7 cards (one per model family: Flash, Pro, Flash Lite, etc.)
- **Authentication**: OAuth credentials (auto-discovered from `~/.gemini/oauth_creds.json`) or custom OAuth client ID/secret for refresh.

## Setup Methods Quick Overview

The Gemini collector supports the following authentication methods:

1.  **OAuth Credentials (Preferred)**:
    *   **Method**: Log in via the Gemini CLI, which stores credentials in `~/.gemini/oauth_creds.json`. Runway will automatically discover and use these.
    *   **Details**: See [Primary: Google Cloud Code API](#primary-google-cloud-code-api).

2.  **Custom OAuth Client ID/Secret**: Required if token auto-refresh fails.
    *   **Method**: Set `GEMINI_OAUTH_CLIENT_ID` and `GEMINI_OAUTH_CLIENT_SECRET` environment variables.
    *   **Details**: See [Configuration](#configuration) and [Troubleshooting: Token refresh fails](#token-refresh-fails).

## Data Sources

### Primary: Google Cloud Code API
**Endpoints:**
- `cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` (get tier + project)
- `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` (get quotas)

**Auth:** OAuth token (auto-refreshed via `oauth2.googleapis.com/token`)

**Key Discovery:** Project parameter required to get gemini-3 model quotas

### Secondary: Local Session Logs
**Location:** `~/.gemini/tmp/sessions/*.jsonl`
**Tracks:** prompt_tokens + completion_tokens (24h rolling window)

## Output Format

```python
{
    "service": "Gemini 2.5 Flash",
    "icon": "🔵",
    "remaining": "0%",           # % used (0% = fresh quota)
    "unit": "used",
    "reset": "Resets at 13:44",
    "health": "good",
    "pace": "Stable",
    "detail": "100% remaining | Model: gemini-2.5-flash",
    "used_value": 0.0,
    "limit_value": 100.0,
    "is_unlimited": False,
    "unit_type": "percent",
    "reset_at": "2026-04-08T13:44:00+00:00",
    "data_source": "oauth",
    "tier": "free",              # "free" | "pro" | "ultra"
    "usage_url": "https://one.google.com/settings",
    "updated_at": "2026-04-08T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_OAUTH_CLIENT_ID` | Optional* | OAuth client ID for token refresh |
| `GEMINI_OAUTH_CLIENT_SECRET` | Optional* | OAuth client secret |

*Required for token refresh. Extract from Gemini CLI: `node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js`

## Sidecar Support

Sidecar extracts OAuth token from `~/.gemini/oauth_creds.json`. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### Missing gemini-3 models
**Cause:** Project parameter not provided
**Fix:** Ensure `loadCodeAssist` returns valid `cloudaicompanionProject`

### Token refresh fails
**Cause:** Missing client ID/secret
**Fix:** Set `GEMINI_OAUTH_CLIENT_ID` and `GEMINI_OAUTH_CLIENT_SECRET` env vars

### Always shows 0% used
**Expected:** Standard tier typically shows 100% remaining (0% used) as quotas are generous

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/gemini.py` | Main collector |
| `scripts/sidecar.py` | Sidecar implementation |

## References

- **Gemini:** https://gemini.google.com
- **Google Cloud:** https://console.cloud.google.com

*Last updated: 2026-04-10*