# Ollama Cloud Collector

**File:** `app/services/collectors/ollama.py`

The Ollama provider supports two collection strategies:

- **`api`** (preferred when an API key is available): bearer `Authorization: Bearer …`
  against `GET https://ollama.com/api/usage`. `limits.monthly.usage` is accepted
  only as a finite fraction from 0 to 1 and displayed as a percentage.
  The API response does not provide a quota reset, so the card leaves reset
  and pace unavailable. Missing or unsupported values fall through to web.
  Cleaner than the HTML scrape and unaffected by WorkOS redesign churn.
- **`web`** (fallback): scrape `https://ollama.com/settings` for included-usage meters
  (one card per usage meter; window type comes from a concrete meter label —
  `Hourly usage` → session, `Weekly usage` → weekly — else the reset horizon;
  vague plan-name labels like `Free usage` only count when no reset timestamp
  is shown).

## Overview

- **Collection Strategies**: `api` (bearer) → `web` (cookie scrape) fallback.
- **Cards**: one monthly card (api path) or one card per usage meter (web path).
- **Authentication**: bearer API key (preferred), or browser cookie (web, pushed by
  the sidecar) / `OLLAMA_SESSION_TOKEN` (web). Saved account credentials take
  precedence, followed by sidecar values and then server environment variables.

## Setup Methods Quick Overview

The Ollama collector supports the following authentication methods:

1.  **Browser Cookie**: Extracted from your local browser by the sidecar.
    *   **Method**: Log in to `https://ollama.com/settings` in your browser (Chrome/Safari/Firefox/Edge). Runway will automatically pick up the session cookie.
    *   **Details**: See [Primary: Ollama Plan & Settings Page](#primary-ollama-plan--settings-page).

2.  **Session Token (OLLAMA_SESSION_TOKEN)**:
    *   **Method**: If running headless, set the session token as `OLLAMA_SESSION_TOKEN` on the server.
    *   **Details**: See [Configuration](#configuration).

## Data Source

### Primary: Ollama Plan & Settings Page
**Endpoint:** `https://ollama.com/settings`
**Auth:** Browser `__Secure-session` (or `session`) cookie — a bare value or a full `Cookie` header is accepted
**Details:** The collector fetches the HTML, reads each `data-usage-track` meter's `aria-label` (e.g. `Free usage 0% used`) for percentage used, and the nearby `data-time` for reset timestamps; legacy labeled usage blocks (`Session usage` / `Weekly usage`) remain supported as a fallback. Plan tier (Free/Pro/Max) is read from the `Included usage` (legacy: `Cloud Usage`) heading badge. Cookie sources are checked in order: account settings → sidecar push → server `OLLAMA_SESSION_TOKEN`.

## Output Format

```python
# Example output format (similar to other collectors)
{
    "service": "Ollama (Session Usage)",
    "icon": "🦙",
    "remaining": "70%",
    "unit": "capacity",
    "reset": "in 1h 30m",
    "health": "good",
    "pace": "Stable",
    "detail": "30% used (Free Plan)",
    "used_value": 30.0,
    "limit_value": 100.0,
    "is_unlimited": False,
    "unit_type": "percent",
    "reset_at": "2026-04-07T12:00:00+00:00",
    "data_source": "web",
    "tier": "free",
    "usage_url": "https://ollama.com/settings",
    "updated_at": "2026-04-07T10:30:00+00:00",
}
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `OLLAMA_API_KEY` | Optional | Ollama Cloud key for the `/api/usage` quota endpoint; server env applies to the default account |
| `OLLAMA_SESSION_TOKEN` | Optional | Ollama session cookie value or full `Cookie` header; server env applies to the default account |

The API strategy requires a finite usage fraction between 0 and 1. If the
endpoint returns a count or another shape, collection falls back to the web
settings page. That page may require a browser session cookie.

*Either auto-discovery or environment variable required.

## Sidecar Support

Sidecar can extract cookies. See [sidecar documentation](../sidecar.md).

## OpenCode traffic

Ollama Cloud usage driven through [OpenCode](opencode.md) is folded into these
same cards rather than showing up as a separate `opencode-ollama` entry. See
`_OC_CANONICAL_MAP` in `scripts/sidecar_pkg/event_extractors/opencode.py` —
events with `providerID: "ollama-cloud"` are retagged to
`provider_id="ollama"` with their own account_id kept (pass-through, since the
Ollama Cloud quota card resolves to the same email via
`resolve_account_id(account_label)`) at ingest, with their logged $0 free-tier
cost dropped so the server reprices them from the table below (currently no
pricing rows — folded events land at $0, same as the existing logged value).

### OpenCode CLI auto-discovery

If you have the opencode CLI installed and you've configured an Ollama Cloud
provider there, Runway auto-discovers the API key from
`~/.local/share/opencode/auth.json["ollama-cloud"].key` (or
`~/.opencode/auth.json`) on every host that runs a sidecar. The same key
opencode uses for its own `ollama-cloud` backend feeds this collector's
`/api/usage` quota call automatically — no `OLLAMA_SESSION_TOKEN` env var or
manual cookie paste needed on hosts with the opencode CLI. When the API key
is unavailable the collector transparently falls back to the cookie scrape
of `/settings`, so legacy browser-cookie setups continue to work.

Already-ingested events under the old `opencode-ollama` id need a one-time
migration, with the server **stopped** (SQLite is single-writer) and
`APP_HOST=127.0.0.1`. The default-vs-email duplication must be collapsed first
(some pre-fix ingest streams pushed the same message under both accounts):

```bash
# 1. Dedupe the default-vs-email split left over from before the sidecar
#    resolved account identity consistently — see
#    scripts/collapse_default_account_events.py for the full rationale.
python scripts/collapse_default_account_events.py --provider opencode-ollama --dry-run
python scripts/collapse_default_account_events.py --provider opencode-ollama --apply

# 2. Retag provider to "ollama" and rebuild rollups for both ids.
python scripts/reclassify_opencode_providers.py --providers opencode-ollama --dry-run
python scripts/reclassify_opencode_providers.py --providers opencode-ollama --apply
```

No pricing reprice step is needed (no provider_pricing rows exist for ollama,
cost is $0 today) and no gauge-card cleanup script is needed (no `latest_usage`
rows exist for `opencode-ollama` — the synthesized fleet entry derives from
`usage_events` directly and disappears once the events move).

## Troubleshooting

### "API Key detected" error
**Fix:**
You likely pasted an API key (e.g., starting with `sk-`) into the session cookie field.

**How to get the correct session cookie:**
If the cookie doesn't appear in the "Application" tab, use the **Network** tab:
1. Log in to `https://ollama.com/settings` in your browser.
2. Open Developer Tools (`F12`) and go to the **Network** tab.
3. Refresh the page.
4. Click on the request named **`settings`**.
5. Look at the **Request Headers** section for the **`Cookie`** header.
6. Copy either the full value of the `Cookie` header or just the `__Secure-session=...` entry (a bare value with no cookie name also works — Runway sends it under both `session` and `__Secure-session`).
7. Paste this into the Runway settings for Ollama.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/ollama.py` | Main collector |
| `app/core/browser_cookies.py` | Browser cookie extraction logic |

## References

- **Ollama:** `https://ollama.com/settings`
