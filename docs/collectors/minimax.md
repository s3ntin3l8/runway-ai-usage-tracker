# MiniMax Collector

**File:** `app/services/collectors/minimax.py`

MiniMax coding-plan quota collector.

## Overview

- **Collection Strategy**: api (REST) only — no fallback tier. The HTML coding-plan page
  (`platform.minimax.io/user-center/payment/coding-plan`) is a client-rendered SPA and can't
  be scraped, so a former regex-based fallback was retired.
- **Cards**: Two cards per poll — a `session` (5h rolling) card and a `weekly` card. There is
  no monthly window.
- **Authentication**: `MINIMAX_API_KEY`

## Data Source

### api (Coding Plan API)
**Endpoint:** `https://api.minimax.io/v1/coding_plan/remains` (default — international host).
Set `MINIMAX_HOST=minimaxi.com` to switch to the China host
(`https://api.minimaxi.com/v1/coding_plan/remains`). `MINIMAX_HOST` must be a **bare domain**
— the collector prefixes it with `api.` (and, for the dashboard link, `platform.`) itself.
**Auth:** Bearer token.

**Response** (real payload, one entry per quota bucket in `model_remains[]` — not one per
billable model; `"general"` covers the text/coding models this collector surfaces, `"video"`
is a separate entitlement it doesn't):

```json
{
  "model_remains": [
    {
      "model_name": "general",
      "start_time": 1788361200000, "end_time": 1788379200000, "remains_time": 4692260,
      "current_interval_total_count": 0, "current_interval_usage_count": 0,
      "current_interval_status": 1, "current_interval_remaining_percent": 100,
      "weekly_start_time": 1788134400000, "weekly_end_time": 1788739200000,
      "current_weekly_total_count": 0, "current_weekly_usage_count": 0,
      "current_weekly_status": 1, "current_weekly_remaining_percent": 100
    },
    {
      "model_name": "video",
      "current_interval_status": 3,
      "...": "..."
    }
  ],
  "base_resp": { "status_code": 0, "status_msg": "success" }
}
```

Field notes (undocumented by MiniMax; derived from observation of a live account):

- `*_remaining_percent` is **remaining**, not used — the collector inverts it
  (`pct_used = 100 - remaining_percent`) when `total_count` is still 0 (a fresh/unused plan).
  Once `total_count > 0`, `pct_used = usage_count / total_count * 100` takes precedence.
- `current_interval_status` (and `current_weekly_status`) bundles two different things.
  `{1, 2}` means "you own this bucket" — `1` = available, `2` = exhausted (0% remaining, observed
  live once real traffic depleted the "general" bucket); both are shown. `3` (observed on the
  `"video"` entry, always 100% remaining regardless of usage) means "not part of your plan" and is
  excluded. MiniMax doesn't document the enum, so this is observed behavior, not a documented
  contract — an earlier version of this collector treated status `2` the same as `3` and silently
  hid the card at 0% remaining, exactly when a user most needs to see it.
- `end_time` / `weekly_end_time` (ms epoch) map directly to `reset_at` — verified against
  `remains_time` (`remains_time == end_time - now`), so there's no need to reconstruct from it.
- The weekly block (`weekly_start_time` / `weekly_end_time` / `current_weekly_*`) is **identical
  across every `model_remains` entry** — one account-level weekly quota, not per-model — so the
  collector emits exactly one weekly card, taken from the first entitled entry.
- `/v1/token_plan/remains` is a newer path MiniMax documents on the same host; this collector
  still uses `/v1/coding_plan/remains`, which is confirmed working.

## Output Format

Two cards, `window_type="session"` (5h) and `window_type="weekly"`:

```python
{
    "service_name": "MiniMax",
    "icon": "🤖",
    "remaining": "90.0%",
    "unit": "capacity",
    "reset": "32m",
    "health": "good",
    "pace": "Sustainable",
    "detail": "40/400 prompts (5h) [API]",
    "used_value": 40.0,
    "limit_value": 400.0,
    "unit_type": "requests",
    "pct_used": 10.0,
    "window_type": "session",
    "model_id": None,
    "reset_at": "2026-09-02T20:00:00+00:00",
    "tier": None,
    "data_source": "api",
    "input_source": "config",
    "usage_url": "https://platform.minimax.io/user-center/payment/coding-plan",
    "updated_at": "2026-09-02T19:27:00+00:00",
}
```

`tier` is always `None` — MiniMax doesn't return a plan/tier name, and third-party sources
disagree on the vocabulary for this product (Plus/Max/Ultra vs. Starter/Pro/Max). The allowance
is surfaced in `detail` instead once `current_interval_total_count`/`current_weekly_total_count`
start reporting real numbers (`0/0` on a fresh, unused subscription — displayed as `"—/—"`).

## Pricing (notional cost)

`app/services/pricing_seed.py` seeds pay-as-you-go API rates (USD/Mtok) for MiniMax models, so
Coding-Plan usage (subscription — $0 actual cost) gets the same "what this would have cost on
the API" figure that Claude subscription usage gets against Claude API rates:

| Model | Input | Output | Cache read | Confidence |
|---|---|---|---|---|
| `MiniMax-M3` | $0.30 | $1.20 | $0.06 | High — cross-checked against two sources |
| `MiniMax-M2.7` | $0.30 | $1.20 | $0.06 | Medium |
| `MiniMax-M2.5` | $0.30 | $1.20 | $0.03 | Medium — one source instead quotes $0.27/$0.95 |
| `MiniMax-M2` | $0.255 | $1.02 | $0.03 | Low — single source, not cross-checked |

M3 has an undocumented pricing cliff above 512K input tokens (doubles to $0.60/$2.40, cache
read $0.12); this isn't modeled per-event (the schema has no context-length tiering), so a
>512K-token call will undercount. Verify the legacy (M2/M2.5/M2.7) rates against
`platform.minimax.io`'s pricing page before relying on them for anything beyond a rough figure.

## OpenCode traffic

MiniMax coding-plan usage driven through [OpenCode](opencode.md) is folded into these same
cards rather than showing up as a separate `opencode-minimax-coding-plan` entry. See
`_OC_CANONICAL_MAP` in `scripts/sidecar_pkg/event_extractors/opencode.py` — events with
`providerID: "minimax-coding-plan"` are retagged to `provider_id="minimax"`,
`account_id="default"` (matching this collector's own account identity, since MiniMax is
API-key-only and has no account email) at ingest, with their logged $0 cost dropped so the
server prices them from the table above instead.

Already-ingested events under the old `opencode-minimax-coding-plan` id need a one-time
migration, with the server **stopped** (SQLite is single-writer) and `APP_HOST=127.0.0.1`:

```bash
# 1. Start the server once first if this is a fresh DB — seed_pricing_table() only
#    runs inside init_db() at startup, so the MiniMax pricing rows above don't exist
#    until the server has started at least once. Then stop it again before migrating.
# 2. Scope the rescan to just the MiniMax fold-in (skips the much larger legacy
#    opencode/opencode-free backlog, which reclassify_opencode_providers.py can also
#    migrate, but that's a separate, unrelated cleanup):
python scripts/reclassify_opencode_providers.py \
    --providers opencode-minimax-coding-plan --apply
# 3. Price the retagged events from the table above:
python scripts/recost_events.py --provider minimax
# 4. Clear the stale pre-fix gauge cards (see scripts/cleanup_minimax_monthly_cards.py):
python scripts/cleanup_minimax_monthly_cards.py --apply
# 5. Restart the server.
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `MINIMAX_API_KEY` | Yes | MiniMax API key |
| `MINIMAX_HOST` | Optional | Bare-domain host override. Default `minimax.io` (international); set to `minimaxi.com` for the China platform. |

## Sidecar Support

Sidecar uses the same API key. No aggregation needed. See [sidecar documentation](../sidecar.md).

## Troubleshooting

### "Missing API key" error
**Fix:**
1. Get key from https://api.minimax.io (international) or https://api.minimaxi.com (China)
2. `export MINIMAX_API_KEY="minimax-..."`
3. If you registered on the China platform, also set `MINIMAX_HOST=minimaxi.com`.

### API connection failed
**Cause:** Network error or invalid API key.
**Fix:** Verify your internet connection and API key validity.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/collectors/minimax.py` | Main collector |
| `app/services/pricing_seed.py` | Notional API pricing for Coding-Plan usage |
| `scripts/sidecar_pkg/event_extractors/opencode.py` | Folds OpenCode-driven MiniMax traffic into this provider |

## References

- **MiniMax (international):** https://www.minimax.io
- **MiniMax (China):** https://www.minimaxi.com
- **API Documentation:** https://api.minimax.io/document/main (or https://api.minimaxi.com/document/main for the China platform)

*Last updated: 2026-09-02*
