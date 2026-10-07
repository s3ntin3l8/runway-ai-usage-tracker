# ChatGPT Collector

**File:** `app/services/collectors/chatgpt.py` (server-side: `web` strategy on `wham/usage`)
**Sidecar:** `scripts/sidecar.py` + `sidecar_app/` (local-source strategies: Codex CLI RPC, session logs)

ChatGPT Codex quota collector with an `api` (executed against the web endpoint with an OAuth bearer or cookie) tier and a `local` enrichment tier that runs inside the sidecar. The server itself does not shell out to `codex` or scan `~/.codex/sessions/`.

## Overview

- **Collection Strategy**: api (Web API / Cookie) → local (CLI RPC / Logs, via sidecar)
- **Cards**: 1-2 cards per `rate_limit` window Codex reports — a `session` (5h) card from `primary_window`, plus a `weekly` card from `secondary_window` when the plan reports one (Plus/Pro). Free/Go plans that report only `primary_window` still get a single card.
- **Authentication**: `CHATGPT_OAUTH_TOKEN` (api), `~/.codex/auth.json` (sidecar-discovered), or Chrome cookies (web).

## Setup Methods Quick Overview

The ChatGPT collector supports multiple authentication and data collection methods:

1.  **OAuth Token / Session Cookie**:
    *   **Method**: Log in to [chatgpt.com](https://chatgpt.com) in Chrome for automatic extraction, OR manually paste the `__Secure-next-auth.session-token` into the **Runway Settings** UI.
    *   **Details**: See [Primary: ChatGPT wham/usage API](#primary-chatgpt-whamusage-api) and [Troubleshooting: "No logs/auth" error](#no-logsauth-error).

2.  **Codex CLI Cache (`~/.codex/auth.json`)**:
    *   **Method**: Log in using the `codex` CLI (`codex auth login`) and run the sidecar on that machine: the sidecar discovers `~/.codex/auth.json` and pushes the token. The server never reads that file from its own host.
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

Codex's `wham/usage` payload carries up to two independent windows under
`rate_limit`: `primary_window` (a rolling 5h session window) and
`secondary_window` (a 7d weekly window, reported by Plus/Pro plans). Each
present window becomes its own card, classified by its own
`limit_window_seconds` rather than by `plan_type` — so a plan we haven't seen
a payload for still classifies correctly. A window missing
`limit_window_seconds` falls back to `window_type: "monthly"` (the legacy
free/Go shape).

```python
[
    {
        "service_name": "ChatGPT",
        "variant": "Codex",
        "window_type": "session",
        "icon": "💬",
        "remaining": "97.0%",
        "unit": "remaining",
        "reset": "Resets in 3h 51m",
        "health": "good",
        "pace": "Stable",
        "detail": "PLUS Account · user@example.com · 3.0% used (5h)",
        "used_value": 3.0,
        "limit_value": 100.0,
        "pct_used": 3.0,
        "unit_type": "percent",
        "reset_at": "2026-01-08T02:19:35+00:00",
        "data_source": "api",
        "input_source": "config",
        "tier": "plus",
        "updated_at": "2026-01-07T22:26:04+00:00",
    },
    {
        "service_name": "ChatGPT",
        "variant": "Codex",
        "window_type": "weekly",
        "icon": "💬",
        "remaining": "100.0%",
        "unit": "remaining",
        "reset": "Resets in 6d 22h",
        "health": "good",
        "pace": "Stable",
        "detail": "PLUS Account · user@example.com · 0.0% used (weekly)",
        "used_value": 0.0,
        "limit_value": 100.0,
        "pct_used": 0.0,
        "unit_type": "percent",
        "reset_at": "2026-01-14T22:19:35+00:00",
        "data_source": "api",
        "input_source": "config",
        "tier": "plus",
        "updated_at": "2026-01-07T22:26:04+00:00",
    },
]
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `CHATGPT_OAUTH_TOKEN` | Optional | OAuth token for API access |

## Sidecar Support

Sidecar extracts token from `~/.codex/auth.json`. See [sidecar documentation](../sidecar.md).

The Codex access token lives ten days and the refresh token rotates on every refresh, so the server never refreshes a login a sidecar pushed. It renews when `codex` next runs, or automatically when the sidecar runs with `--keep-alive` ([`docs/sidecar.md`](../sidecar.md#keep-alive-for-antigravity-claude-code-codex-and-xai-opt-in)), which refreshes the token a day before expiry and writes it back to `auth.json`.

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

*Last updated: 2026-09-09*
