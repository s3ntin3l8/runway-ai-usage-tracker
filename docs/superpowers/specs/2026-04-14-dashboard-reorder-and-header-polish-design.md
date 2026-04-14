# Dashboard reorder + header polish — design

## Context

The dashboard orders provider sections and cards by a fixed rule (worst-health first, then alphabetical). Users want to pin their most-watched providers and cards in the order that matches their mental model. While we're in the header we also clean up three longstanding papercuts:

1. The header controls mix an emoji (`☀️`), an SVG icon, and a text label ("SYNC") — visually inconsistent.
2. A browser refresh always snaps back to Dashboard, losing the current tab (History, Fleet, Settings).

This spec covers all three changes because they all land in the same header region and share one short frontend PR.

## Goals

- Let the user reorder provider sections and reorder cards within a section; persist across devices (server-side).
- Unify the header controls as icon-only grayscale buttons with consistent size/weight.
- Keep the user on the same tab across browser reloads.

## Non-goals

- Reordering cards *across* provider sections. Cards stay pinned to their provider.
- Per-user preferences. This is a single-user app; layout is global, stored in `SystemConfig`.
- Any keyboard-based reordering UI (future work — drag-only for v1).
- A separate "Save" button. Each drop persists immediately.

## Architecture

### Data model

One new column on `SystemConfig` (single global row):

```sql
ALTER TABLE system_config ADD COLUMN dashboard_layout_json TEXT
```

JSON shape:

```json
{
  "provider_order": ["anthropic", "gemini", "github"],
  "card_orders": {
    "anthropic": ["<card-key>", "<card-key>"],
    "gemini":    ["<card-key>"]
  }
}
```

- **Provider key** = `provider_id` string (matches how `app.js` currently groups).
- **Card key** = `${account_id}|${service_name}|${model_id ?? ""}|${window_type}` — the natural composite identity of a card. Built by a pure helper in `frontend/js/layout.js` (`cardKey(card)`) and matched exactly on the backend — backend just stores/returns the blob.

### Ordering rule

Applied on the frontend, before rendering, on every data update:

1. Split providers/cards into **pinned** (present in saved order) and **unpinned** (not).
2. Render pinned in saved order; append unpinned in the current default sort (health-first, then alphabetical/usage).
3. Entries present in saved order but absent from live data are silently ignored.

This means newly-added providers or newly-appearing cards always surface without user intervention, and stale keys never cause render errors.

### Backend

Two endpoints in `app/api/endpoints/system.py`, no admin key (parity with existing UI-facing settings):

| Method | Path | Body / Response |
|---|---|---|
| `GET` | `/api/v1/system/dashboard-layout` | Returns stored JSON. Empty-layout default (`{provider_order: [], card_orders: {}}`) if unset. |
| `PUT` | `/api/v1/system/dashboard-layout` | Accepts the same shape; validates via `DashboardLayout` Pydantic model; writes to `SystemConfig.dashboard_layout_json` as a JSON string. |

Validation: `provider_order` is `list[str]`, `card_orders` is `dict[str, list[str]]`. All string-typed, no length cap (layouts are tiny).

### Frontend

- **Sortable.js** (MIT, ~24 KB) lazy-loaded from a CDN the same way Chart.js is in `frontend/js/charts.js:74` (existing `ensureChartJS` pattern). A new `frontend/js/sortable.js` module exports `ensureSortable()`.
- Edit-mode toggle lives in the header (see UX below). On enter: initialise Sortable on the provider strip and on each section's card grid. On exit: destroy those Sortable instances.
- On every drop: read current DOM order, rebuild the layout JSON, `PUT` it, and update the in-memory cache and `localStorage` (`runway_layout`). Local cache is read on boot for instant render while the server GET is in flight.

### Tab persistence

URL-hash-driven routing, no server-side piece:

- On boot, `app.js` reads `location.hash`; if it matches a known view id, calls `switchView(hash)`; otherwise defaults to dashboard.
- `switchView(viewId)` sets `location.hash = viewId` (no page navigation).
- A `hashchange` listener re-runs `switchView` so browser back/forward works.

### Header redesign

Replace the current mixed-style header buttons with a single row of icon-only, 32×32, grayscale buttons (`text-zinc-500` / hover `text-zinc-200`, 14px single-stroke SVG — matches the existing refresh icon):

| Button | Icon | Tooltip | Active state |
|---|---|---|---|
| Refresh | existing refresh-arrows SVG (drop the "SYNC" label) | "Force sync all providers" | spin animation while polling |
| Theme | sun ↔ moon SVG swap (replaces `☀️` emoji) | "Toggle bright/dark" | icon reflects current mode |
| Edit layout *(new)* | pencil-on-square SVG | "Edit layout" / "Done" | filled pill when active |

The `#last-updated` timestamp stays to the right of the button row.

### Edit-mode visual affordances

When active:
- Pencil button renders as a filled pill with label "Done".
- Provider sections and cards get `cursor: grab` and a 1px dashed `border-zinc-700/40` outline.
- A small 6-dot "grip" SVG appears in the top-right of each card and next to each provider section header; purely decorative — the whole element is draggable via Sortable's default handle.
- While dragging, Sortable applies its own drop-indicator class.

Clicking outside a drag does nothing. Only the Done button exits edit mode.

## Files

### Backend

- `app/models/db.py` — add `dashboard_layout_json: str | None` to `SystemConfig`.
- `app/core/db.py` — add `"ALTER TABLE system_config ADD COLUMN dashboard_layout_json TEXT"` to `_run_migrations`.
- `app/models/schemas.py` — `DashboardLayout(BaseModel)`.
- `app/api/endpoints/system.py` — `GET` + `PUT /api/v1/system/dashboard-layout`.
- `tests/integration/test_dashboard_layout.py` *(new)* — empty default, round-trip, malformed-input rejection.

### Frontend

- `frontend/index.html` — rework header button block (inline three SVG icons, drop "SYNC" text, drop `☀️` emoji).
- `frontend/js/app.js` — hash-based routing in `switchView` + `DOMContentLoaded`; edit-mode toggle handler; apply saved layout before every render.
- `frontend/js/views/dashboard.js` — call shared ordering helper (same ordering logic as `app.js`).
- `frontend/js/components.js` — add `data-provider-id` to `.provider-section`, `data-card-key` to each rendered card.
- `frontend/js/state.js` — `editMode: false` flag, `layout` cache, new `runway_layout` localStorage key.
- `frontend/js/api.js` — `getDashboardLayout()`, `putDashboardLayout(layout)`.
- `frontend/js/sortable.js` *(new)* — lazy-load Sortable.js from CDN, mirroring `ensureChartJS`.
- `frontend/js/layout.js` *(new)* — pure helpers: `cardKey(card)`, `applyLayout(data, layout)`, `extractCurrentOrder(gridElement)`.

## Verification

Automated:
- `uv run pytest tests/integration/test_dashboard_layout.py` — empty GET returns defaults; PUT + GET round-trip; malformed PUT → 422.
- Existing `tests/integration/test_system.py` unaffected.

Manual (follow in order):
1. Reload dashboard — order matches current health-first rule (no layout saved yet).
2. Click Edit layout → pencil becomes filled pill, grid gains dashed outlines, grip glyphs appear.
3. Drag "Gemini" section above "Anthropic" → order updates, request fires to `PUT /api/v1/system/dashboard-layout`.
4. Drag a card within a section → order updates, request fires.
5. Click Done → edit-mode affordances go away, order persists.
6. Hard reload → order reflects saved layout.
7. Switch to History tab → URL becomes `…/#history`. Hard reload → stays on History.
8. Use browser back → returns to dashboard.
9. Disconnect a provider so a card/section is missing → remaining items stay in their saved order; no console errors.
10. Add a new provider (e.g. via settings) → new section appears at the end of the list; existing order preserved.
