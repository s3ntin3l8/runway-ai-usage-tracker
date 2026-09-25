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
to a provider account — unless one of the identity sources below resolves
them first. The sidecar reports the key under a **key-scoped origin**
(`path:…/auth.json#<fingerprint>`, see *Account identity*), so two hosts —
or one host after a key rotation — never share a tag.

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

## Account Identity

### Why the CLI key has no identity of its own

Runway needs to know *whose* `oc_sk_…` key it found. For the OpenCode CLI
there is no such answer in the credential, and none online either. Every
source was checked and comes back empty:

| Source checked | What it returned |
|---|---|
| `opencode auth list` | Display names only — no email, no account id |
| `opencode debug info` / `debug paths` | Paths and versions; nothing identity-shaped |
| `opencode.db` `account(email, …)` table | Empty (0 rows) and not linked to the API key |
| `anomalyco/opencode` `console/app/src/routes/zen/go/v1/*` | `{chat, messages, models, responses, systemone, usage}` — no identity route |
| `GET https://opencode.ai/zen/go/v1/usage` | `{"usage": {rolling, weekly, monthly}}` only |
| `GET https://opencode.ai/console/api/user` | Cookie-only; 401 with a bearer token |
| `~/.local/state/opencode` | Nothing |

So the sidecar cannot *derive* an account for a discovered key. It must
also never *inherit* one: `path:/home/u/.local/share/opencode/auth.json`
is identical on every host with the same username, and identical before
and after a key rotation. If that were the origin, two different keys
would share one operator tag and silently land on each other's account.

### Key-scoped origins

Origins for opencode are therefore suffixed with a 12-hex fingerprint of
the *key* (`credential_fingerprint`, PBKDF2-HMAC-SHA256, salt
`runway-credential-fp-v1`):

```
path:/home/alice/.local/share/opencode/auth.json#495fa9c614ce
env:OPENCODE_API_KEY#495fa9c614ce
```

Only the fingerprint crosses the wire — never the key. The server builds
the same value from a key pasted into `provider_configs`, under
`provider:opencode#<fingerprint>` (it does not know the sidecar's paths;
the sidecar does not know which account row matched).

**Key rotation produces a new fingerprint, hence a new origin.** The old
tag stays on the old credential and the rotated key lands in Untagged
Credentials until it is tagged again or matched by a pasted key below.

### Resolution cascade

The sidecar resolves a token card's account most-specific-first; whatever
remains unresolved is blocked and shown in **Fleet → Untagged
Credentials**:

| # | Tier | Source |
|---|---|---|
| 0 | `OPENCODE_ACCOUNT_LABEL` | Host-local, explicit. Local evidence outranks every server-side hint — the same precedence `_opencode_account_email` applies to events. |
| 1 | Keyed origin tag | Operator tag written against this exact credential (`…#<fingerprint>`). |
| 1b | Fingerprint hint | `provider:opencode#<fingerprint>` → account, when the same key is in `provider_configs`. Not a guess, so it ships even in multi-host deployments. |
| 2 | Legacy plain tag | `path:…` tag written by an older sidecar, before origins were fingerprinted. |
| 3 | Provider-wide auto-hint | `provider:opencode` → account, **gated** (below). |
| — | Untagged | Nothing resolved; the credential is not shipped. |

### The tier-3 gate

`provider:opencode` is the last resort and can only mean one thing: "the
server has exactly one account configured, so it is probably that one."
Before honouring it, the sidecar compares the discovered key against
`account.json`'s active `opencode-go` record — the only local evidence
that can contradict the guess:

| `account.json` state | Result |
|---|---|
| Absent, unreadable, no active record for `opencode-go` | `"unknown"` — no contradicting evidence, the hint applies (preserves behaviour for older CLI installs). |
| Active record holds this exact key | `"single"` — one live account, the discovered key is it. Hint applies. |
| Active record holds a *different* key | `"ambiguous"` — the credential on disk is not the account the CLI says is active. The hint is withheld, a warning is logged, the card stays Untagged until tagged against its key-scoped origin. |

### Assigning a key

- **Paste it** in Providers → opencode (source 3). The server then
  recognises the key by fingerprint on the next sidecar cycle (tier 1b).
- **Or tag its origin** in Fleet → Untagged Credentials. The origin line
  shows the full `…#<fingerprint>` string.
- **Or set `OPENCODE_ACCOUNT_LABEL=<email>`** on the sidecar host when
  that machine's CLI key always belongs to one account (tier 0).

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
| `invalid_config` | `invalid_config` | Set the OpenCode workspace ID in settings. |
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
| `scripts/sidecar_pkg/identity.py`, `app/services/account_identity.py` | `credential_fingerprint` + keyed-origin helpers (mirrored on both sides) |
| `app/api/endpoints/fleet.py` (`_fingerprinted_credential_hints`) | Server-side tier-1b hint |
| `tests/unit/test_opencode_credential_identity.py`, `tests/integration/test_fleet_credentials.py` | Identity-source and isolation coverage |

## References

- **CodexBar opencode.md** — the canonical description of the two
  endpoints the collector targets.
- **OpenCode:** https://opencode.ai
