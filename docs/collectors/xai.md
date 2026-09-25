# xAI (Grok) Collector

**File:** `app/services/collectors/xai.py`

Quota collector for xAI's consumer Grok subscriptions (SuperGrok / SuperGrok Heavy). Reads the OAuth access bearer the opencode CLI stores in `~/.local/share/opencode/auth.json["xai"]["access"]` and surfaces credits + on-demand usage against the documented CLI-proxy billing endpoint.

## Overview

- **Collection Strategy**: `api` (bearer) — single-strategy.
- **Cards**: one credits card (`weekly` or `monthly` window from the API's `currentPeriod.type`) plus an optional on-demand card when the user has a non-zero `onDemandCap`.
- **Authentication**: xAI OAuth access token (auto-extracted from opencode `auth.json` or pasted into provider settings). A pasted bearer is stored as `xai_access`; only the CLI file's actual refresh token is stored as `xai_refresh`. Runway does not refresh tokens itself. The `oc_sk_…` developer API key surface (https://api.x.ai/v1/…) is a different auth — not for the consumer subscription; Runway doesn't use it for this collector.

## Auth Sources

### Primary: opencode CLI auto-discovery

If you have the [opencode CLI](opencode.md) installed and logged in to xAI there, the sidecar's standard JSON file-rule dispatcher maps `xai.access` to `xai_access` and `xai.refresh` to `xai_refresh` from `~/.local/share/opencode/auth.json` (or `~/.opencode/auth.json`) on every host that runs a sidecar. No custom parser is involved.

```json
{
  "xai": {
    "type": "oauth",
    "access": "<JWT>",
    "refresh": "<opaque-refresh-token>",
    "expires": 1788205330512
  }
}
```

When the access token is a JWT with a readable `exp` claim, the collector checks it before making requests. Expired tokens short-circuit to an `auth_failed` card pointing the operator at the CLI re-login flow. An undecodable token is sent to the endpoint, where a 401 or 403 produces the same card. The refresh is handled by the CLI itself — Runway doesn't refresh server-side.

If you paste a bearer in the provider settings, Runway stores it as `xai_access` only. A manually pasted access token is not treated as a refresh token.

### OpenCode CLI events -> xai provider

The opencode event extractor retags events with `providerID: "xai"` onto the canonical `provider_id: "xai"` (see `_OC_CANONICAL_MAP` in `scripts/sidecar_pkg/event_extractors/opencode.py`). Token-usage events from opencode calling Grok models (e.g. `grok-4-fast`, `grok-4.20-non-reasoning`) land on the same `xai` quota card fed by the auto-extracted OAuth token.

## Endpoints

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `https://cli-chat-proxy.grok.com/v1/billing?format=credits` | GET | `Authorization: Bearer <oauth>` + `x-xai-token-auth: xai-grok-cli` | Quota gauge: `creditUsagePercent`, `onDemandUsed`, `onDemandCap`, `currentPeriod.{start,end,type}`, `productUsage[]`. |
| `https://cli-chat-proxy.grok.com/v1/settings` | GET | same | Best-effort enrichment: `subscription_tier_display` ("SuperGrok" vs "SuperGrok Heavy"). 5s budget; failures degrade silently. |

The CodexBar docs (https://github.com/steipete/CodexBar/blob/main/docs/grok.md) document a richer fallback chain (`grok agent stdio` ACP JSON-RPC, `grok.com` gRPC-web with WKE keypair, browser cookies). Runway sticks to the OAuth-bearer CLI-proxy path because the other surfaces require either a local `grok` CLI binary (which Runway can't reasonably shell out to from a server) or a browser-held WKE keypair the sidecar can't obtain.

## Card Schema

```python
{
    "service_name": "xAI",
    "icon": "🤖",
    "remaining": "82.0%",  # credits card
    "unit": "remaining",
    "reset": "Sustainable",
    "health": "good",
    "pace": "Sustainable",
    "detail": "Credits used",  # or "$0.12 of $500.00 on-demand used"
    "used_value": 18.0,  # percent for credits, USD for on-demand
    "limit_value": 100.0,  # percent for credits, USD for on-demand
    "pct_used": 18.0,
    "is_unlimited": False,
    "unit_type": "percent",  # or "currency" for on-demand
    "currency": None,  # or "USD" for on-demand
    "reset_at": "2026-10-01T23:01:54+00:00",
    "account_label": "<team email>",
    "window_type": "weekly",  # or "monthly" per currentPeriod.type
    "provider_id": "xai",
    "tier": "SuperGrok Heavy",  # None when settings enrichment failed
    "data_source": "api",
    "input_source": "sidecar",
    "usage_url": "https://console.x.ai",
    "updated_at": "<ISO 8601>",
}
```

## Window Mapping

| `currentPeriod.type` | Internal `window_type` |
|---|---|
| `USAGE_PERIOD_TYPE_WEEKLY` | `weekly` |
| `USAGE_PERIOD_TYPE_MONTHLY` | `monthly` |
| (anything else) | `monthly` (fallback) |

xAI doesn't publish a 5-hour rolling window — weekly and monthly are the only two surfaces.

## Failure Modes

| Reason | Error type | Card message |
|---|---|---|
| `invalid_api_key` | `auth_failed` | "xAI session expired — re-login opencode CLI" |
| `parse_error` | `parse_error` | "xAI quota collection failed." (default message — `_error_handler` only specializes `invalid_api_key`) |
| (timeout/network) | (silent — base collector retries) | — |

## Migration from the Stub

The first iteration of this collector was a token-status stub that surfaced an error card when the OAuth JWT was expired but did not fetch quota. The current collector fetches `/v1/billing?format=credits` + `/v1/settings` and surfaces the actual quota while keeping the expired-token error path.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/xai.py` | Main collector |
| `scripts/sidecar.py` | Baked xAI file rule, using the generic nested-key mapping dispatcher |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Retags opencode events with `providerID: "xai"` onto `provider_id: "xai"` |
| `app/core/registry.json` (`providers.xai`) | UI label + sidecar rule mirror |

## References

- **xAI:** https://x.ai
- **CodexBar docs:** https://github.com/steipete/CodexBar/blob/main/docs/grok.md (documents the fallback chain Runway doesn't currently use — `grok agent stdio`, `grok.com` gRPC-web, browser cookies)
- **OpenCode CLI:** https://opencode.ai
