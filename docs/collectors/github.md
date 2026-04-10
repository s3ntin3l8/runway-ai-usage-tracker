# GitHub Copilot Collector

**File:** `app/services/collectors/github.py`

GitHub Copilot quota collector with tier-aware multi-endpoint strategy.

## Overview

- **Collection Strategy**: Copilot Internal APIs → Standard GitHub API (fallback)
- **Cards**: 2 cards (Completions, Chat for free tier) or 3 cards (Premium, Chat, Autocomplete for Pro)
- **Authentication**: `GITHUB_TOKEN` environment variable

## Data Sources

### Primary: GitHub Copilot Internal APIs
**Endpoints:**
- `api.github.com/copilot_internal/v2/token` (free/limited tier quotas)
- `api.github.com/copilot_internal/user` (Pro/Enterprise snapshots)

**Auth:** `Authorization: token <GITHUB_TOKEN>`
**Headers:** VS Code Copilot extension headers for reliability

### Fallback: GitHub API Rate Limits
**Endpoint:** `api.github.com/rate_limit`
**Trigger:** When Copilot endpoints unavailable

## Output Format

```python
{
    "service": "Copilot (Completions)",
    "icon": "🐙",
    "remaining": "45",
    "unit": "/ 100",
    "reset": "in 2h 30m",
    "health": "good",
    "pace": "Stable",
    "detail": "45/100 requests left • Free Tier",
    "used_value": 55.0,
    "limit_value": 100.0,
    "is_unlimited": False,
    "unit_type": "requests",
    "reset_at": "2026-04-08T00:00:00+00:00",
    "data_source": "api",
    "tier": "free",
    "usage_url": "https://github.com/settings/copilot/features",
    "updated_at": "2026-04-07T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | GitHub personal access token with `copilot` scope |
| `GITHUB_CLIENT_ID` | No | Client ID for OAuth Device Flow (default: `Iv1.b507a08c87ecfe98`) |

## GitHub OAuth Setup

If you prefer to use the interactive "Connect GitHub" flow instead of a static `GITHUB_TOKEN`:

1.  Create a new **OAuth App** at [GitHub Developer Settings](https://github.com/settings/developers).
2.  Set the **Homepage URL** to any valid URL (e.g., `http://localhost:8765`).
3.  **IMPORTANT:** Check the box **"Enable Device Flow"**. Without this, the login will return a `404 Not Found` error.
4.  Copy the **Client ID** and add it to your `.env` file:
    ```bash
    GITHUB_CLIENT_ID=your_new_client_id_here
    ```
5.  Restart Runway.

## Sidecar Support

Sidecar uses lighter implementation with `/rate_limit` endpoint only. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### No Copilot data returned
**Check:**
1. `echo $GITHUB_TOKEN` - is it set?
2. Token has `copilot` scope?
3. User has active Copilot subscription?

### 401/403 errors
**Fix:**
1. Regenerate token at https://github.com/settings/tokens
2. Ensure "Copilot" scope is granted
3. Verify subscription at https://github.com/settings/copilot

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/github.py` | Main collector |
| `scripts/sidecar.py` | Sidecar (rate limit only) |

## References

- **GitHub Copilot:** https://github.com/features/copilot
- **Token Settings:** https://github.com/settings/tokens

*Last updated: 2026-04-10*
