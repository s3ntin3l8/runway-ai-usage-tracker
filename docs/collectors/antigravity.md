# Antigravity Collector

**File:** `scripts/sidecar.py` (functions `_ag_*` and `collect_antigravity_lsp`)

Sidecar-only quota collector for the Antigravity IDE. The server has no antigravity collector — all detection happens on the host where the IDE runs and is shipped via `/fleet/ingest`.

## Overview

- **Collection Strategy**: local LSP probe (primary), local JSON file (fallback) — both run inside the sidecar
- **Cards**: 1-3 cards per model/credit type
- **Authentication**: auto-discovered from the running IDE (CSRF token from process args)

## Setup Methods Quick Overview

The sidecar obtains quota information using two strategies, in order:

1.  **LSP Probing (Primary)**:
    *   Locates the active Antigravity Language Server process, extracts its CSRF tokens and listening ports, and POSTs to `/exa.language_server_pb.LanguageServerService/GetUserStatus`.
    *   Requires the Antigravity IDE to be running on the same host as the sidecar.

2.  **Local JSON Quota File (Fallback)**:
    *   Reads `quota.json` written by the IDE under the platform data dir.
    *   Used only when the LSP probe finds no process.

## Data Source

### Tier 1: local (LSP Probing)
**Mechanism:** The sidecar detects running Antigravity processes, identifies their LSP ports and CSRF tokens, and directly queries user status.
**Auth:** Uses internal tokens scraped from the running process command line.
**Behavior:** Preferred data source when the IDE is active.

### Tier 2: local (JSON Quota File)
**Location:** Platform-specific `quota.json` file.
**Behavior:** Fallback when no active IDE process is detected.

## Output Format

```python
# LSP source
{
    "service_name": "claude-sonnet-4-5",           # model label — no "AG: " prefix
    "icon": "🛸",
    "remaining": "75.5%",
    "unit": "capacity",
    "reset": "in 2h 15m",                          # computed from resetTime
    "reset_at": "2026-04-15T12:00:00+00:00",       # ISO 8601 from quotaInfo.resetTime
    "health": "good",
    "pace": "Continuous",
    "detail": "Pro | user@example.com [LSP]",
    "data_source": "local",
    "provider_id": "antigravity",
    "account_id": "user@example.com",              # email used as account_id
    "account_label": "user@example.com",
    "model_id": "claude-sonnet-4-5-20251001",
    "used_value": 24.5,
    "limit_value": 100.0,
    "unit_type": "percent",
    "window_type": "session"
}
```

## Configuration

No configuration required. The sidecar auto-discovers everything from the running IDE.

## Troubleshooting

### No Antigravity cards in dashboard
**Cause:** LSP process not found/probed, or local file not found, or sidecar can't reach the server.
**Fix:**
1.  Ensure the Antigravity IDE is running and actively being used.
2.  Verify the LSP process is running (check system process monitor).
3.  Check sidecar logs for `[antigravity] LSP returned N card(s)`.
4.  Check server logs for `Stored N local cards into LatestUsage from <sidecar-id>` — if missing, the cards were dropped at ingest (verify HMAC/auth is configured).
5.  If still no LSP, check the local quota file (fallback):
    -   **Linux:** `ls ~/.local/share/antigravity/state/quota.json`
    -   **macOS:** `ls ~/Library/Application\ Support/antigravity/state/quota.json`
    -   **Windows:** Path not confirmed — LSP probing is the primary method on Windows

### LSP connection failed / Timeout
**Cause:** Firewall, incorrect port, or LSP not listening on expected port.
**Fix:**
1.  Check firewall settings allowing local connections to Antigravity IDE.
2.  Restart Antigravity IDE.

### Shows 0% for all models (from local file)
**Cause:** Quota file has zero values.
**Fix:** Use models in IDE to generate usage data.

## Related Files

| File | Purpose |
|------|---------|
| `scripts/sidecar.py` | Sidecar collector (LSP probe + file fallback) |
| `app/api/endpoints/fleet.py` | Server ingest endpoint |
| `app/services/accumulator.py` | Card → `LatestUsage` upsert |

## References

- **Antigravity IDE:** https://antigravity.ai

> **Note:** File updated when user checks quota in IDE or at IDE startup.
