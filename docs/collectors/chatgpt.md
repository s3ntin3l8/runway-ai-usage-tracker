# ChatGPT Collector

**File:** `app/services/collectors/chatgpt.py`

ChatGPT Codex quota collector with OAuth-backed API and local session cache fallback.

## Overview

- **Collection Strategy**: OAuth API → Local session cache
- **Cards**: 1 card (primary window usage)
- **Authentication**: `CHATGPT_OAUTH_TOKEN` env var → `~/.codex/auth.json` → Chrome browser cookies

## Setup Methods Quick Overview

The ChatGPT collector supports multiple authentication methods:

1.  **OAuth Token (CHATGPT_OAUTH_TOKEN)**:
    *   **Method**: Set the `CHATGPT_OAUTH_TOKEN` environment variable with a valid OAuth token.
    *   **Details**: See [Configuration](#configuration) and [Troubleshooting: "No logs/auth" error](#no-logsauth-error).

2.  **Codex CLI Cache (`~/.codex/auth.json`)**:
    *   **Method**: Log in using the `codex` CLI (`codex auth login`). Runway will automatically discover the token from `~/.codex/auth.json`.
    *   **Details**: See [Primary: ChatGPT wham/usage API](#primary-chatgpt-whamusage-api) and [Troubleshooting: "No logs/auth" error](#no-logsauth-error).

3.  **Chrome Browser Cookie**:
    *   **Method**: Log in to [chatgpt.com](https://chatgpt.com) in Chrome. Runway will attempt to extract the `__Secure-next-auth.session-token` cookie and exchange it for a Bearer token.
    *   **Details**: See [Primary: ChatGPT wham/usage API](#primary-chatgpt-whamusage-api) and [Troubleshooting: "No logs/auth" error](#no-logsauth-error).

## Data Sources

### Primary: ChatGPT wham/usage API
**Endpoint:** `chatgpt.com/backend-api/wham/usage`
**Auth:** Bearer token

**Token Sources (priority order):**
1. `CHATGPT_OAUTH_TOKEN` environment variable
2. `~/.codex/auth.json` (Codex CLI cache)
3. Chrome browser cookie (`__Secure-next-auth.session-token`) — auto-exchanged for Bearer token

### Secondary: Local Session Cache
**Location:** `~/.codex/sessions/*.jsonl`
**Tracks:** `used_percent`, `resets_at` from latest session file

## Output Format

```python
{
    "service": "ChatGPT Codex",
    "icon": "💬",
    "remaining": "54.5%",
    "unit": "remaining",
    "reset": "Resets in 4h 30m",
    "health": "good",
    "pace": "Stable",
    "detail": "API: wham/usage",
    "used_value": 45.5,
    "limit_value": 100.0,
    "is_unlimited": False,
    "unit_type": "percent",
    "reset_at": "2026-04-07T15:00:00+00:00",
    "data_source": "oauth",
    "tier": None,
    "usage_url": None,
    "updated_at": "2026-04-07T10:30:00+00:00"
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `CHATGPT_OAUTH_TOKEN` | Optional | OAuth token for API access |

## Sidecar Support

Sidecar extracts token from `~/.codex/auth.json`. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "No logs/auth" error
**Fix:**
1. `export CHATGPT_OAUTH_TOKEN="your-token"`
2. Or install Codex CLI: `npm install -g @openai/codex && codex auth login`
3. Or log in to chatgpt.com in Chrome — session cookie is extracted automatically

### API Error (401/403 on accounts endpoint)
**Expected:** The `accounts/check` endpoint may return 403 depending on account type — this is non-fatal and usage data is still collected from `wham/usage`.

### API Error (401) on wham/usage
**Fix:** Token expired - re-authenticate with Codex CLI or set a fresh `CHATGPT_OAUTH_TOKEN`

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/chatgpt.py` | Main collector |
| `scripts/sidecar.py` | Sidecar implementation |

## References

- **Codex CLI:** https://github.com/openai/codex

*Last updated: 2026-04-10*
