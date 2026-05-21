# ChatGPT Collector

**File:** `app/services/collectors/chatgpt.py` (server-side: `web` strategy on `wham/usage`)
**Sidecar:** `scripts/sidecar.py` + `sidecar_app/` (local-source strategies: Codex CLI RPC, session logs)

ChatGPT Codex quota collector with an `api` (executed against the web endpoint with an OAuth bearer or cookie) tier and a `local` enrichment tier that runs inside the sidecar. The server itself does not shell out to `codex` or scan `~/.codex/sessions/`.

## Overview

- **Collection Strategy**: api (Web API / Cookie) → local (CLI RPC / Logs, via sidecar)
- **Cards**: 1 card (primary window usage)
- **Authentication**: `CHATGPT_OAUTH_TOKEN` (api), `~/.codex/auth.json` (sidecar-discovered), or Chrome cookies (web).

## Setup Methods Quick Overview

The ChatGPT collector supports multiple authentication and data collection methods:

1.  **OAuth Token / Session Cookie**:
    *   **Method**: Log in to [chatgpt.com](https://chatgpt.com) in Chrome for automatic extraction, OR manually paste the `__Secure-next-auth.session-token` into the **Runway Settings** UI.
    *   **Details**: See [Primary: ChatGPT wham/usage API](#primary-chatgpt-whamusage-api) and [Troubleshooting: "No logs/auth" error](#no-logsauth-error).

2.  **Codex CLI Cache (`~/.codex/auth.json`)**:
    *   **Method**: Log in using the `codex` CLI (`codex auth login`). Runway will automatically discover the token from `~/.codex/auth.json`.
    *   **Details**: See [Primary: ChatGPT wham/usage API](#primary-chatgpt-whamusage-api) and [Troubleshooting: "No logs/auth" error](#no-logsauth-error).

3.  **Codex CLI RPC**:
    *   **Method**: Install the `@openai/codex` CLI and ensure it's in your PATH. Runway will execute `codex -s read-only` to get quota data.
    *   **Details**: See [Secondary: Codex CLI RPC](#secondary-codex-cli-rpc) below.

4.  **Local Session Cache**:
    *   **Method**: If the Codex CLI is used, it generates session log files. Runway can read these as a last resort.
    *   **Details**: See [Tertiary: Local Session Cache](#tertiary-local-session-cache) below.

## Data Sources

### Tier 1: api (Web API / Cookie)
**Endpoint:** `chatgpt.com/backend-api/wham/usage`
**Auth:** Bearer token (OAuth) or Session Cookie (Web).
**Behavior:** Primary method for both official tokens and browser-based sessions.

### Tier 2: local (CLI RPC / Logs) — sidecar-only
**Runs in:** the sidecar (`scripts/sidecar.py` / `sidecar_app/`). The server-side collector implements `web` only.
**Mechanism (sidecar):**
- **CLI RPC**: Interfaces with `codex -s read-only` directly.
- **Local Logs**: Parses `~/.codex/sessions/*.jsonl` for historical usage and emits per-message events.
**Behavior:** The sidecar pushes the parsed quota card and per-message events to `/api/v1/fleet/ingest`; the server merges them with the `wham/usage` headline `%`. Cards merged from sidecar-collected data are tagged `data_source=local`, `input_source=sidecar`.

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
    "data_source": "api",
    "input_source": "config",
    "tier": "plus",
    "usage_url": "https://chatgpt.com/codex/settings/usage/",
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
1. Set `export CHATGPT_OAUTH_TOKEN="your-token"`.
2. Or install Codex CLI: `npm install -g @openai/codex` and run `codex auth login`.
3. Or log in to chatgpt.com in Chrome — session cookie is extracted automatically.

### API Error (401/403 on accounts endpoint)
**Expected:** The `accounts/check` endpoint may return 403 depending on account type — this is non-fatal and usage data is still collected from `wham/usage`.

### API Error (401) on wham/usage
**Fix:** Token expired - re-authenticate with Codex CLI or set a fresh `CHATGPT_OAUTH_TOKEN`.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/chatgpt.py` | Main collector |
| `scripts/sidecar.py` | Sidecar implementation |

## References

- **Codex CLI:** https://github.com/openai/codex

## Manual Authentication (DevTools)

If automatic browser extraction is not working (e.g., in Docker or headless environments), you can manually provide authentication:

### 1. OAuth Bearer Token (Recommended)
- **Field in Runway**: **API Key (Bearer Token)**
- **Token Source**: Browser DevTools -> Network -> Filter by `/wham/usage` -> Headers -> `Authorization` header (`Bearer xxx`).
- **Note**: This is the most direct method. Runway will automatically extract your Account ID from this token.

### 2. Session Token
- **Field in Runway**: **Session Cookie (Session Token)**
- **Token Source**: Browser DevTools -> Application -> Cookies -> `https://chatgpt.com` -> `__Secure-next-auth.session-token`.
- **Note**: This is a fallback method. Runway will attempt to exchange this for a Bearer token.

*Last updated: 2026-05-21*
