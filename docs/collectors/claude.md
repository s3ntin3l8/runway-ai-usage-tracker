# Claude Collector

**File:** `app/services/collectors/anthropic.py`

Anthropic Claude quota collector with 4-tier fallback: OAuth API → Web API → Local logs → Error cards.

## Overview

- **Collection Strategy**: OAuth API → Web API (cookies) → Enhanced Local Logs → Error Cards
- **Cards**: 2-5 cards (Session, Weekly, Sonnet, Opus, Extra Usage windows)
- **Authentication**: OAuth token (env var, credentials file, or macOS keychain) OR Chrome cookies

## Data Sources

### Primary: OAuth API
**Endpoint:** `https://api.anthropic.com/api/oauth/usage`
**Auth:** Bearer token from `CLAUDE_CODE_OAUTH_TOKEN`, `~/.claude/.credentials.json`, or macOS keychain
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
| `CLAUDE_CODE_OAUTH_TOKEN` | Optional | OAuth token for API access |
| `CLAUDE_CONFIG_DIR` | Optional | Comma-separated paths to config directories |

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
