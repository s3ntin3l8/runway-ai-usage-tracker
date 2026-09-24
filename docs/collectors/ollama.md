# Ollama Cloud Collector

**File:** `app/services/collectors/ollama.py`

The Ollama provider scrapes the **Plan & Settings** page at `https://ollama.com/settings` to extract included-usage limits (one card per usage meter; the window type is inferred from the meter label, falling back to the reset horizon).

## Overview


- **Collection Strategy**: web (Scraping)
- **Cards**: one card per usage meter (legacy markup: Session and Weekly usage windows)
- **Authentication**: Browser cookie (web, pushed by the sidecar) or `OLLAMA_SESSION_TOKEN` (web).

## Setup Methods Quick Overview

The Ollama collector supports the following authentication methods:

1.  **Browser Cookie**: Extracted from your local browser by the sidecar.
    *   **Method**: Log in to `https://ollama.com/settings` in your browser (Chrome/Safari/Firefox/Edge). Runway will automatically pick up the session cookie.
    *   **Details**: See [Primary: Ollama Plan & Settings Page](#primary-ollama-plan--settings-page).

2.  **Session Token (OLLAMA_SESSION_TOKEN)**:
    *   **Method**: If running headless, obtain your session token from browser Developer Tools and set it as the `OLLAMA_SESSION_TOKEN` environment variable.
    *   **Details**: See [Configuration](#configuration).

## Data Source

### Primary: Ollama Plan & Settings Page
**Endpoint:** `https://ollama.com/settings`
**Auth:** Browser `__Secure-session` (or `session`) cookie — a bare value or a full `Cookie` header is accepted
**Details:** The collector fetches the HTML, reads each `data-usage-track` meter's `aria-label` (e.g. `Free usage 0% used`) for percentage used, and the nearby `data-time` for reset timestamps; legacy labeled usage blocks (`Session usage` / `Weekly usage`) remain supported as a fallback. Plan tier (Free/Pro/Max) is read from the `Included usage` (legacy: `Cloud Usage`) heading badge. Multiple cookie sources are tried in order: settings UI → `OLLAMA_SESSION_TOKEN` → sidecar push.

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
| `OLLAMA_SESSION_TOKEN` | Optional* | Ollama session cookie value or full `Cookie` header (auto-discovered if not set) |

> [!CAUTION]
> **API Keys are not supported**: Ollama Cloud API keys (found at `ollama.com/settings/keys`) cannot be used for quota tracking as there is currently no public API for account usage. You **must** provide a browser session cookie (`__Secure-session`, or a bare value from the `Cookie` header).

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
