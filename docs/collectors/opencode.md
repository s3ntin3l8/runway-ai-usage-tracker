# OpenCode Collector

**File:** `app/services/collectors/opencode.py`

OpenCode quota collector with API-key and session-cookie strategies.

## Overview

- **Strategy:** `api` (primary, bearer token) → `web` (fallback, console session cookies)
- **Cards:** 3 cards per account (5h rolling, 7d weekly, 30d monthly)
- **Auth:** OpenCode Go API key (`oc_sk_…`), or session cookies (`auth` + `__Host-console_session`)

## Auth Sources

Saved provider credentials take precedence for their account, followed by credentials discovered by the sidecar and then server environment variables. Local files and browser cookies are read by the sidecar; the server does not inspect another machine's filesystem.

### 1. CLI discovery

The opencode CLI stores every configured provider's credential in
`~/.local/share/opencode/auth.json`. Runway reads `opencode-go.key` from
this file on hosts where the sidecar runs. Since the file can contain
keys for several provider accounts without reliable account identity,
newly discovered keys appear under Untagged Credentials until assigned
to a provider account.

```
~/.local/share/opencode/auth.json
└── "opencode-go": {"type": "api", "key": "oc_sk_…"}
```

The same extraction works from `~/.opencode/auth.json` (alternate install
location).

### 2. Environment variable

`OPENCODE_API_KEY=oc_sk_…` may be set on the sidecar or server. The
sidecar reports local environment values; a server uses its environment
value for the default account.

### 3. UI / `provider_configs` manual paste

The Providers → opencode page exposes an `OpenCode API Key (api)` field
that encrypts and persists the key into `provider_configs.api_key_encrypted`.
The collector reads the account-scoped value directly, so it remains
available after restart and does not expire with the sidecar token cache.

### 4. Cookie fallback (legacy / non-migrated workspaces)

Two cookies are required: `auth` and `__Host-console_session`. The
sidecar extracts both from the browser on each cycle, and the manual UI
paste accepts a multi-cookie string that gets split into the two
tokens. Without both cookies, the console handshake returns 401 /
`{"_tag":"Unauthorized"}` and the dashboard surfaces an auth_failed
error card.

## Endpoints

### Primary (new)

- `GET https://opencode.ai/zen/go/v1/usage` — bearer auth.
  Returns `{usage: {rolling, weekly, monthly: {percent, status, resetsAt}}}`.
- `GET https://opencode.ai/console/api/go/status` — cookie auth with
  `x-org-id`. Returns subscription meters with usage, limits, and resets.

### Fallback (cookie)

- `GET https://opencode.ai/console/api/orgs` — lists workspaces
  (`[{id, name}]`). Cookie auth only; bearer returns 401.
- `GET https://opencode.ai/console/api/go/status` with `x-org-id` header —
  same body shape as the bearer variant.

When the cookie account has one Go workspace, it is selected automatically.
When several workspaces have Go access, enter that workspace's ID in the
OpenCode account settings. The collector never guesses from list order.

## Card Schema

```python
{
    "service_name": "OpenCode",
    "icon": "⚡",
    "remaining": f"${remaining:.2f}",
    "unit": f"${limit:.0f} limit",
    "reset": "5h" | "7d" | "30d",
    "health": "good" | "warning" | "critical",
    "pace": PaceCalculator.estimate_longevity(pct, reset_at),
    "detail": f"${used:.2f} used ({pct:.1f}%) · OpenCode Go API",
    "used_value": used,
    "limit_value": limit,
    "pct_used": pct,
    "is_unlimited": False,
    "unit_type": "currency",
    "currency": "USD",
    "account_label": "<email>" | "",
    "reset_at": "<ISO 8601>" | None,
    "window_type": "session" | "weekly" | "monthly",
    "provider_id": "opencode",
    "tier": "Go",
    "data_source": "api" | "web",
    "input_source": "sidecar" | "config" | "server",
    "usage_url": "https://opencode.ai/console/usage",
    "updated_at": "<ISO 8601>",
}
```

## Window Mapping

The OpenCode API uses different keys depending on the endpoint:

| API shape | Internal `window_type` | Default limit (USD) |
|---|---|---|
| `fiveHour` / `rolling` | `session` | $12 |
| `week` / `weekly` | `weekly` | $30 |
| `month` / `monthly` | `monthly` | $60 |

`window_type` is the canonical Runway enum used for card identity across
the dashboard.

## Failure Modes

The collector no longer silently returns `[]`. Every failure mode is
mapped to an `_last_error_reason` that `_error_handler` translates into
a visible error card with `error_type`:

| Reason | Error type | Card message |
|---|---|---|
| `missing_api_key` | `auth_failed` | OpenCode session expired — paste a fresh `oc_sk_…` API key… |
| `invalid_api_key` | `auth_failed` | OpenCode session expired — paste a fresh `oc_sk_…` API key… |
| `missing_cookies` | `auth_failed` | OpenCode session expired — paste a fresh `oc_sk_…` API key… |
| `session_invalid` | `auth_failed` | OpenCode session expired — paste a fresh `oc_sk_…` API key… |
| `no_workspace` | `parse_error` | OpenCode: no workspace found for the configured account. |
| `invalid_config` | `invalid_config` | Set the OpenCode workspace ID for this account. |
| `api_unavailable` | `api_error` | OpenCode: usage API unreachable. Will retry on next cycle. |
| (other) | `unknown` | OpenCode quota collection failed. |

Auth errors identify expired or missing credentials. Untagged CLI keys
must first be assigned to an OpenCode account in Settings.

## Migration from the Legacy `_server` Path

The old collector called `/_server?id=def3997…` with the `auth` Iron
cookie. That endpoint is no longer the right surface for migrated
workspaces: opencode now answers with a 302-encoded redirect to
`/console/login` (encoded as a `new Response(null, {status:302, location:…})`
in the RSC stream), or returns the SPA shell. The collector detects
both as `_last_error_reason = "session_invalid"` and surfaces an error
card.

The `auth` cookie alone is also no longer sufficient — the console
handshake requires `__Host-console_session` too. The sidecar's cookie
rule now extracts both names.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/opencode.py` | Main collector |
| `scripts/sidecar.py` (opencode block, ~:400-440) | Sidecar credential rules |
| `app/core/registry.json` (`providers.opencode`) | UI labels + sidecar rule mirror |
| `app/services/collector_manager.py` (`_sync_manual_config_to_cache`) | DB → token-cache bridge for manual UI pastes |
| `app/api/endpoints/system.py` (`upsert_provider_config_for_account`) | Opencode-specific cookie / API-key parsing |

## References

- **CodexBar opencode.md** — the canonical description of the two
  endpoints the collector targets.
- **OpenCode:** https://opencode.ai
