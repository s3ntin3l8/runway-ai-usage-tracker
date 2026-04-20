# GitHub Copilot Collector

**File:** `app/services/collectors/github.py`

GitHub Copilot quota collector with tier-aware multi-endpoint strategy.

## Overview

- **Collection Strategy**: api (Copilot Internal) → api (GitHub Rate Limit fallback)
- **Cards**: 2 cards (Completions, Chat) or 3 cards (Premium, Chat, Autocomplete)
- **Authentication**: `GITHUB_TOKEN` (api) or `gh` CLI credentials (api)

## Setup Methods Quick Overview

The GitHub Copilot collector supports multiple authentication methods:

1.  **Personal Access Token (PAT)**: Provide a static GitHub Personal Access Token (PAT) with `copilot` scope.
    *   **Method**: Set the `GITHUB_TOKEN` environment variable.
    *   **Details**: Refer to the [Configuration section](#configuration) for `GITHUB_TOKEN` and the [Authentication section](#authentication) in the Overview.

2.  **OAuth Device Flow**: Interactively log in via GitHub's OAuth Device Flow to obtain a token.
    *   **Method 1 (Default)**: Uses Runway's bundled public `GITHUB_CLIENT_ID`.
    *   **Method 2 (Custom)**: Use your own GitHub OAuth App by setting `GITHUB_CLIENT_ID`.
    *   **Details**: See the [GitHub OAuth Setup section](#github-oauth-setup) below.

3.  **`gh` CLI Credential Discovery**: Automatically discover credentials from the `gh` CLI's configuration.
    *   **Method**: Log in via `gh auth login`, and Runway will read the `oauth_token` from `~/.config/gh/hosts.yml` (or Windows equivalent).
    *   **App/Sidecar**: Supported by both the main Runway app and the Sidecar.
    *   **Details**: See the [Credential Discovery section](#credential-discovery) below.

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
    "input_source": "manual",
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

Runway uses the public GitHub OAuth Client ID (`Iv1.b507a08c87ecfe98`) to enable the interactive "Connect GitHub" flow. If you prefer to use your own OAuth App instead of a static `GITHUB_TOKEN`:

1.  Create a new **OAuth App** at [GitHub Developer Settings](https://github.com/settings/developers).
2.  Set the **Homepage URL** to any valid URL (e.g., `http://localhost:8765`).
3.  **IMPORTANT:** Check the box **"Enable Device Flow"**. Without this, the login will return a `404 Not Found` error.
4.  Copy the **Client ID** and add it to your `.env` file:
    ```bash
    GITHUB_CLIENT_ID=your_new_client_id_here
    ```
5.  Restart Runway.

## Credential Discovery

Runway supports automatic credential discovery for GitHub. If you have logged in via the `gh` CLI (`gh auth login`), Runway will attempt to read your `oauth_token` from the `gh` CLI's configuration file (`~/.config/gh/hosts.yml` on Linux/macOS, or `%APPDATA%\GitHub CLI\hosts.yml` on Windows).

**Custom Config Directory**:
The default location for Runway's configuration files (including where GitHub OAuth tokens are saved) is platform-specific (e.g., `~/.config/runway-tracker` on Linux). You can override this location by setting the `RUNWAY_CONFIG_DIR` environment variable to an absolute path. This is particularly useful for Docker or custom multi-host deployments.

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
