# GitHub Copilot Collector

**File:** `app/services/collectors/github.py`

GitHub Copilot quota collector. One endpoint (`copilot_internal/user`) returns the plan and either quota snapshots (paid) or limited/monthly quotas (free).

## Overview

- **Collection Strategy**: api (`copilot_internal/user`); no fallback endpoint
- **Cards**: 2 cards (Completions, Chat) or 3 cards (Premium, Chat, Autocomplete)
- **Authentication**: a GitHub OAuth token (device login, `gh`, or an editor sign-in) or a PAT; see [Credential Discovery](#credential-discovery)

## Setup Methods Quick Overview

The GitHub Copilot collector supports multiple authentication methods:

1.  **Token**: Provide a static GitHub token. An OAuth token (from `gh auth login` or the device login) works best; a classic PAT may be rejected by the Copilot endpoints.
    *   **Method**: Set the `GITHUB_TOKEN` environment variable.
    *   **Details**: Refer to the [Configuration section](#configuration) for `GITHUB_TOKEN` and the [Authentication section](#authentication) in the Overview.

2.  **OAuth Device Flow**: Interactively log in via GitHub's OAuth Device Flow to obtain a token.
    *   **Method 1 (Default)**: Uses Runway's bundled public `GITHUB_CLIENT_ID`.
    *   **Method 2 (Custom)**: Use your own GitHub OAuth App by setting `GITHUB_CLIENT_ID`.
    *   **Details**: See the [GitHub OAuth Setup section](#github-oauth-setup) below.

3.  **`gh` CLI Credential Discovery**: Automatically discover credentials from the `gh` CLI's configuration.
    *   **Method**: Log in via `gh auth login`, and the sidecar reads the `oauth_token` from `~/.config/gh/hosts.yml` (or Windows equivalent) or the keyring and pushes it. The server never reads `hosts.yml` from its own host.
    *   **App/Sidecar**: Sidecar only (`make dev` alone does not see it; use `make dev-all`).
    *   **Details**: See the [Credential Discovery section](#credential-discovery) below.

## Account identity

A GitHub account is keyed by its **login** (`/user` → `login`), never by email:
the login is returned for every token type and never changes. The email (from
`/user` or `/user/emails`) is only the display label. The server host's own
`git config user.email` and `gh` login are deliberately *not* consulted — it identifies the host,
not the token. `resolve_account_id` treats GitHub as login-keyed, so an email
label cannot re-key an explicitly identified account.

## Data Sources

### GitHub Copilot internal API
**Endpoint:** `api.github.com/copilot_internal/user` (plan, quota snapshots, and the free-tier `limited_user_quotas` / `monthly_quotas`)

**Auth:** `Authorization: token <token>`
**Headers:** VS Code Copilot extension headers for reliability

Identity comes from `api.github.com/user` (and `/user/emails` for the label).

`copilot_internal/v2/token` is deliberately not called: it only mints a short-lived model-proxy token and carries no totals, so it added a secret to cache and redact for no data (#519).

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
    "input_source": "config",
    "tier": "free",
    "usage_url": "https://github.com/settings/copilot/features",
    "updated_at": "2026-04-07T10:30:00+00:00",
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` / `GH_TOKEN` | One credential source is enough | GitHub OAuth token (preferred) or PAT; `GH_TOKEN` is the `gh` CLI's variable and is read the same way |
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

GitHub credentials come from these sources. The first one found by the server (settings key, then env, then Runway's own `github_oauth.json`) is used by the collector; every machine-reported one is kept as its own row in Settings → Credentials.

| Source | Detected by | Notes |
|---|---|---|
| Settings key | server | A token pasted in the provider dialog. |
| `GITHUB_TOKEN` / `GH_TOKEN` | server and sidecar | Environment variable. |
| Sign in with GitHub (device flow) | server | Button in the GitHub account dialog. The token is stored encrypted at `<config dir>/github_oauth.json` and listed as a server credential; it counts as evidence for the account while the file exists, and Disconnect deletes it. Sidecars never ship this file. |
| `gh` CLI `hosts.yml` | sidecar | `~/.config/gh/hosts.yml` (Linux/macOS) or `%APPDATA%\GitHub CLI\hosts.yml`; spellings of the same file are read once. |
| `gh` CLI keyring | sidecar | `gh auth token`, for `gh` ≥ 2.40, which keeps the token in the OS keyring and leaves `hosts.yml` without one. Skipped when it returns a token already found above. |
| Editor Copilot sign-in files | sidecar | `apps.json` / `hosts.json` under `~/.config/github-copilot` (or `%LOCALAPPDATA%\github-copilot`), written by JetBrains, Neovim (`copilot.vim`/`copilot.lua`), Xcode and Zed. `github.com` entries only; the file's `user` is not used as the account id. The three known client ids are tried first, then any other `github.com:<client id>` entry. With several entries the first by sorted key is used. The same token in more than one file counts once. |
| Copilot CLI plaintext token | sidecar | `~/.copilot/config.json` (JSON with `//` comments), `authTokens` / `copilotTokens` entries for `https://github.com:<login>`. The CLI keeps its token in the OS keychain when it can, so this file only holds a token on a machine without one (typically headless Linux); elsewhere it is read and found empty without logging. The login in the key is not used as the account id. `COPILOT_HOME` is not honoured. |
| Windows Credential Manager `github.com` | sidecar | |

Not detected: the VS Code Copilot extension (its sign-in lives in VS Code's encrypted secret storage and is not readable), the Copilot CLI's keychain entry, GitHub Enterprise Cloud (`*.ghe.com`) sign-ins, browser cookies.

If a machine's GitHub credential disappears, the sidecar log (`~/.config/runway/sidecar/sidecar.log`) says `file read but no credential keys found` once per file when a rule's file exists but holds no token (a logged-out `gh`, or a token moved to the keyring).

**Custom Config Directory**:
The default location for Runway's configuration files (including where GitHub OAuth tokens are saved) is platform-specific (e.g., `~/.config/runway` on Linux). You can override this location by setting the `RUNWAY_CONFIG_DIR` environment variable to an absolute path. This is particularly useful for Docker or custom multi-host deployments.

## Sidecar Support

The sidecar only extracts and forwards GitHub credentials; it never calls the GitHub API. The server verifies the account with `/user`. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### No Copilot data returned
**Check:**
1. `echo $GITHUB_TOKEN` - is it set?
2. Is the token an OAuth token for an account with Copilot (a classic PAT may be rejected)?
3. User has active Copilot subscription?

### 401/403 errors
**Fix:**
1. Regenerate token at https://github.com/settings/tokens
2. Prefer `gh auth login` or the Sign in with GitHub button over a classic PAT
3. Verify subscription at https://github.com/settings/copilot

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/github.py` | Main collector |
| `scripts/sidecar.py` | Sidecar (credential extraction only) |

## References

- **GitHub Copilot:** https://github.com/features/copilot
- **Token Settings:** https://github.com/settings/tokens

*Last updated: 2026-04-10*
