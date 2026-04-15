# Claude Collector

**File:** `app/services/collectors/anthropic.py`

Anthropic Claude quota collector with 4-tier fallback: OAuth API → Web API → Local logs → Error cards.

## Overview

- **Collection Strategy**: OAuth API → Web API (cookies) → Enhanced Local Logs → Error Cards
- **Cards**: 2-5 cards (Session, Weekly, Sonnet, Opus, Extra Usage windows)
- **Authentication**: OAuth token (env var, credentials file, macOS keychain) or Chrome cookies

## Setup Methods Quick Overview

The Claude collector supports multiple authentication methods:

1.  **OAuth Token (Preferred)**:
    *   **Method 1**: Set the `CLAUDE_CODE_OAUTH_TOKEN` environment variable.
    *   **Method 2 (Auto-discovered)**: Log in via the `claude` CLI, which creates `~/.config/claude/oauth_creds.json`.
    *   **Method 3 (Auto-discovered, macOS only)**: OAuth token stored in macOS Keychain.
    *   **Details**: See [Primary: OAuth API](#primary-oauth-api) and [Configuration section](#configuration).

2.  **Chrome Cookies**: Used for Web API fallback if OAuth token is unavailable.
    *   **Method**: Log in to [claude.ai](https://claude.ai) in Chrome. Runway will attempt to extract the session cookie.
    *   **Details**: See [Secondary: Web API (Chrome Cookies)](#secondary-web-api-chrome-cookies).

## Data Sources

### Primary: OAuth API
**Endpoint:** `https://api.anthropic.com/api/oauth/usage`
**Auth:** Bearer token from:
- `CLAUDE_CODE_OAUTH_TOKEN` environment variable (preferred)
- `~/.config/claude/oauth_creds.json` (auto-discovered, created by `claude login`)
- macOS Keychain (auto-discovered on macOS)

**Quota Windows:** 5h session, 7d weekly, Sonnet weekly, Opus weekly, Extra usage

### Secondary: Web API (Chrome Cookies)
**Endpoints:** 
- `claude.ai/api/organizations` (get org UUID)
- `claude.ai/api/organizations/{orgId}/usage` (get quotas)

### Tertiary: Local Logs
**Location:** `~/.claude/projects/**/*.jsonl` or `~/.config/claude/projects/**/*.jsonl`
**Tracks:** Input, output, cache_read, cache_creation tokens with deduplication

## Output Format

```python
{
    "service": "Claude (Session Window)",
    "icon": "🟠",
    "remaining": "75.0%",
    "unit": "capacity",
    "reset": "in 2h 15m",
    "health": "good",
    "pace": "Stable",
    "detail": "25.0% used [OAuth]",
    "used_value": 25.0,
    "limit_value": 100.0,
    "is_unlimited": False,
    "unit_type": "percent",
    "reset_at": "2026-04-07T15:00:00+00:00",
    "data_source": "oauth",
    "tier": "pro",
    "usage_url": "https://claude.ai/settings/usage",
    "updated_at": "2026-04-07T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `CLAUDE_CODE_OAUTH_TOKEN` | Optional | OAuth token for API access (auto-discovered from `~/.config/claude/oauth_creds.json` or macOS Keychain if not set) |

**Auto-Discovery:**
- Credentials file: `~/.config/claude/oauth_creds.json` (Linux/Windows) or Keychain (macOS)
- Projects directory: `~/.config/claude/projects/` (auto-discovered for local logs)

## Sidecar Support

Sidecar can extract tokens from `~/.claude/.credentials.json` or macOS keychain. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "No data — OAuth missing & Logs empty"
**Cause:** No authentication available
**Fix:**
1. Set `export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-..."`
2. Or run `claude login` to create credentials file
3. Or login to https://claude.ai in Chrome

### "401 Unauthorized"
**Cause:** Token expired
**Fix:** Automatic refresh should handle this. If refresh fails with `invalid_grant`, run `claude login`

### Cookie decryption fails on macOS
**Fix:** Grant keychain access: `security add-generic-password -s "Chrome Safe Storage" -w`

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/anthropic.py` | Main collector |
| `app/core/browser_cookies.py` | Cross-platform cookie decryption |
| `scripts/sidecar.py` | Sidecar implementation |

## References

- **Claude:** https://claude.ai
- **API Docs:** https://api.anthropic.com

*Last updated: 2026-04-10*
