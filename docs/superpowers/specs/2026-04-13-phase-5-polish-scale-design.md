# Phase 5 — Polish & Scale (5C → 5B → 5A)

**Date:** 2026-04-13
**Status:** Approved for implementation
**Excludes:** 5D (Native Desktop Sidecar) — deferred post-1.0

## Context

Builds on the stable Phase 1–4 core. Ordered backend-first to minimize risk: 5C (pure backend) ships first, 5B (backend + settings UI) second, 5A (largest frontend work) last once the data model is proven stable.

---

## 5C: Intelligent Polling / Sleep Mode

**Files:** `app/services/poller.py`

Dynamic poll interval based on token consumption activity — sleeps when you're not using tokens, wakes immediately when you start again.

- Add `_snapshot_hashes: dict[str, deque]` on `BackgroundPoller` — keyed by `f"{provider_id}:{account_id}"`, each value is a `deque(maxlen=3)` of `hash(used_value, limit_value)` from the last 3 polls
- After each `collect_all()`, compute hashes and append to the deques
- **Dormant condition:** A key is dormant when its deque is full (3 entries) and all 3 are identical
- **Sleep trigger:** When ALL tracked keys are dormant → set `_interval = 7200` (2 hours)
- **Wake trigger:** On any poll where any key's hash changes → set `_interval = 900` (15 min), clear all deques
- Interval is read at the top of the loop (`await asyncio.sleep(self._interval)`), so changes take effect on the next cycle
- No new DB table, no API changes, no frontend changes

---

## 5B: CSV Export + Webhook Alerts

### CSV Export

**Files:** `app/api/endpoints/usage.py`, `frontend/index.html`

- Add `format: str = "json"` query param to `GET /api/v1/usage/history`
- When `format=csv`: return a `StreamingResponse` with `Content-Type: text/csv` and `Content-Disposition: attachment; filename="runway-history-YYYY-MM-DD.csv"`
- Columns: `timestamp, provider_id, account_id, account_label, service_name, used_value, limit_value, unit_type, currency, tier, model_id, window_type, health`
- Frontend: "Download CSV" button on the History tab — an `<a>` tag pointed at `/api/v1/usage/history?format=csv` with the `download` attribute; inherits active filters from `STATE`

### Webhook Alerts

**Files:** `app/models/db.py`, `app/services/webhooks.py` (new), `app/api/endpoints/system.py`, `app/services/poller.py`, `frontend/js/app.js`, `frontend/index.html`

#### Data model — `webhook_configs` table

| Column | Type | Notes |
|:---|:---|:---|
| `id` | `INTEGER` PK | autoincrement |
| `provider_id` | `TEXT` | e.g. `"anthropic"` — `"*"` = global wildcard |
| `threshold_pct` | `REAL` | 0.0–100.0, e.g. `90.0` |
| `url` | `TEXT` | Discord or Slack webhook URL |
| `channel` | `TEXT` | `"discord"` or `"slack"` |
| `active` | `BOOLEAN` | default `true` |
| `last_fired_at` | `DATETIME` | nullable — tracks breach state for fire-once logic |

#### Breach logic (`app/services/webhooks.py`)

- `check_and_fire(cards: list[LimitCard], session: Session)` — called by the poller after each collect cycle
- For each `webhook_config`: find matching cards — `provider_id = "*"` is a global wildcard (matches all providers, evaluated after provider-specific configs); compute `used_pct = used_value / limit_value * 100`
- **Fire condition:** `used_pct >= threshold_pct AND last_fired_at IS NULL`
- **Reset condition:** `used_pct < threshold_pct * 0.85` (15% hysteresis) → set `last_fired_at = NULL`
- **Discord payload:** Rich embed, color `0xED4245` (red), fields for provider / account / current % / threshold
- **Slack payload:** Block Kit with a header block and two context fields
- Both use `httpx.AsyncClient.post()` — fire-and-forget with a short timeout; errors are logged, not raised

#### CRUD API (`app/api/endpoints/system.py`)

- `GET /api/v1/system/webhooks` — list all configs
- `POST /api/v1/system/webhooks` — create config
- `PATCH /api/v1/system/webhooks/{id}` — update threshold / URL / active flag
- `DELETE /api/v1/system/webhooks/{id}` — remove
- `POST /api/v1/system/webhooks/{id}/test` — fire a test payload immediately regardless of threshold

#### Settings UI

- New "Webhook Alerts" section on the Settings tab
- Per-row: provider selector, threshold % input, channel selector (Discord/Slack), URL input, Test button, active toggle
- "Add webhook" button appends a new row

---

## 5A: Chart.js Visualizations

**Files:** `frontend/index.html`, `frontend/js/charts.js` (new), `frontend/js/app.js`, `frontend/css/styles.css`

Token volume trends on the History tab. Primary focus: understanding consumption patterns over time.

**Library:** Chart.js loaded from CDN in `index.html` (no build step required).

**Chart panel** (above the existing history table):
- Toggle between two views: **Bar** (stacked daily by provider) and **Line** (per-provider trend)
- Filter pills reuse the existing Dashboard pattern — provider selector, model_id dropdown
- Date range picker defaulting to last 30 days

**Bar chart:**
- X-axis: daily date buckets aggregated from history snapshots
- Y-axis: `used_value` (tokens for `unit_type == "tokens"`, otherwise raw value)
- Stacked bars, one dataset per provider, consistent color palette
- Tooltip shows per-provider breakdown on hover

**Line chart:**
- One line per provider, same X/Y axes as bar chart
- Dashed horizontal reference line at each provider's `limit_value` (from the latest snapshot)
- Smooth tension: 0.3

**Data flow:**
- `charts.js` exports `initCharts(canvasId)` and `updateCharts(snapshots, filters)`
- `app.js` calls `updateCharts()` whenever history data loads or filters change
- Data source: existing `/api/v1/usage/history` endpoint — no new backend needed

**`charts.js` responsibilities:**
- Parse and bucket raw snapshots by date
- Build Chart.js `datasets` arrays from bucketed data
- Handle empty state: show "No data for selected range" message when datasets are empty
- Destroy and recreate Chart.js instances on filter change to avoid stale rendering state

---

## Verification

- `pytest` passes after each sub-phase (5C, 5B, 5A independently)
- **5C:** Let the app run through 3 unchanged 15-min poll cycles, confirm `_interval` switches to 7200; modify a mock value, confirm instant reset to 900
- **5B:** `curl /api/v1/usage/history?format=csv` returns valid CSV with correct columns; POST to `webhooks/{id}/test` delivers a message to a Discord test channel
- **5A:** Open History tab with existing snapshot data, confirm bar and line charts render; toggle between views; apply a provider filter and confirm chart re-renders with filtered data
