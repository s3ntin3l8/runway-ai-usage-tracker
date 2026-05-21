# Gemini Collector

**File:** `app/services/collectors/gemini.py` (server-side: `api` against Google Cloud Code)
**Sidecar:** `scripts/sidecar.py` + `sidecar_app/` (local enrichment: session logs)

Google Gemini CLI quota collector. The server-side `api` strategy hits Google Cloud Code endpoints with an auto-refreshed OAuth token; the `local` enrichment strategy that reads `~/.gemini/tmp/*/chats/session-*.json` executes inside the sidecar and ships per-message events to the server.

## Overview

- **Collection Strategy**: api (Google Cloud Code) + local enrichment (session logs, via sidecar)
- **Cards**: 1-7 cards (one per model family: Flash, Pro, Flash Lite, etc.)
- **Authentication**: OAuth credentials (api). Local session logs are discovered by the sidecar.

## Setup Methods Quick Overview

The Gemini collector supports the following authentication methods:

1.  **OAuth Credentials (Preferred)**:
    *   **Method**: Log in via the Gemini CLI, which stores credentials in `~/.gemini/oauth_creds.json`. Runway will automatically discover and use these.
    *   **Details**: See [Primary: Google Cloud Code API](#primary-google-cloud-code-api).

2.  **Custom OAuth Client ID/Secret**: Required if token auto-refresh fails.
    *   **Method**: Set `GEMINI_OAUTH_CLIENT_ID` and `GEMINI_OAUTH_CLIENT_SECRET` environment variables.
    *   **Details**: See [Configuration](#configuration) and [Troubleshooting: Token refresh fails](#token-refresh-fails).

## Data Sources

### Primary: api (Google Cloud Code)
**Endpoints:**
- `cloudcode-pa.googleapis.com/...:loadCodeAssist` (project discovery)
- `cloudcode-pa.googleapis.com/...:retrieveUserQuota` (quotas)
**Auth:** OAuth token (auto-refreshed via Google).
**Behavior:** Primary source for model-specific quotas (Flash, Pro, etc.).

### Enrichment: local (Session Logs) — sidecar-only
**Runs in:** the sidecar. The server-side collector is HTTP-only.
**Location (sidecar):** `~/.gemini/tmp/*/chats/session-*.json`
**Tracks:** prompt + completion tokens, cached tokens, thoughts tokens.
**Behavior:** The sidecar parses local session logs and pushes per-message events to `/api/v1/fleet/ingest`. The server merges these with the API quota card — token breakdown and session counts come from this tier. Enrichment rows are tagged `data_source=local`, `input_source=sidecar`.

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
    "data_source": "api",
    "input_source": "config",
    "tier": "pro",
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

*Last updated: 2026-05-21*