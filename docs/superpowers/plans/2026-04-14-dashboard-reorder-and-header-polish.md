# Dashboard Reorder + Header Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user reorder provider summary cards on the dashboard (and per-service cards inside the provider modal), persist that order server-side, unify the header control icons, and keep the user on the same tab across reloads.

**Architecture:** A new `dashboard_layout_json` column on the existing `SystemConfig` table stores a small JSON blob (`provider_order` + `card_orders`). Two new endpoints (`GET`/`PUT /api/v1/system/dashboard-layout`) serve it. Frontend lazy-loads Sortable.js from a CDN (mirroring the existing `ensureChartJS` pattern), renders an edit-mode toggle in the header, and applies the saved order in a pure helper (`frontend/js/layout.js`) before every render. Tab persistence piggybacks on `location.hash`, zero backend work.

**Tech Stack:** FastAPI + SQLModel + SQLite (backend), vanilla JS ES modules + Tailwind (frontend), Sortable.js 1.15.x via CDN, pytest for backend tests. No JS test harness exists in this repo — frontend tasks include explicit manual-verification steps.

---

## File Structure

**Backend (modify):**
- `app/models/db.py` — add one optional column to `SystemConfig`.
- `app/core/db.py` — append one additive `ALTER TABLE` migration.
- `app/api/endpoints/system.py` — add `_DashboardLayout` Pydantic model, `GET` + `PUT /dashboard-layout` routes.
- `tests/integration/test_dashboard_layout.py` *(new)* — empty-default GET, round-trip PUT, malformed PUT.

**Frontend (create):**
- `frontend/js/layout.js` — pure helpers: `cardKey(card)`, `applyLayout(data, layout)`, `extractProviderOrder(gridEl)`, `extractCardOrder(containerEl)`.
- `frontend/js/sortable.js` — `ensureSortable()` lazy CDN loader (mirrors `ensureChartJS`).

**Frontend (modify):**
- `frontend/index.html` — replace `☀️`/SYNC-text buttons with a unified icon row + new Edit-layout button; inline three SVG icons.
- `frontend/js/api.js` — two new functions: `getDashboardLayout()`, `putDashboardLayout(layout)`.
- `frontend/js/state.js` — add `editMode`, `layout`, hydrate from `runway_layout` localStorage.
- `frontend/js/app.js` — hash routing in `switchView` + boot, edit-mode toggle wiring, apply `STATE.layout` in `renderGrid`, Sortable init on enter-edit.
- `frontend/js/components.js` — `buildProviderSummaryCard` emits `data-provider-id`; `buildCard` emits `data-card-key`. Both gain a `.drag-handle` grip glyph that's visible only in edit mode.
- `frontend/js/views/dashboard.js` — same `applyLayout` call before its own render path.
- `frontend/index.html` / a small block in an existing CSS file — `.edit-mode` visual affordances (dashed outline, grip visibility).

---

## Task 1: Backend — schema + migration

**Files:**
- Modify: `app/models/db.py`
- Modify: `app/core/db.py`

- [ ] **Step 1.1: Add the column to `SystemConfig`**

Edit `app/models/db.py` — append this field to the `SystemConfig` class (around line 144):

```python
    dashboard_layout_json: str | None = None
```

- [ ] **Step 1.2: Append the additive migration**

Edit `app/core/db.py` — append to the `migrations` list in `_run_migrations` (around line 56):

```python
        # SystemConfig gained dashboard_layout_json (user-reorder persistence)
        "ALTER TABLE system_config ADD COLUMN dashboard_layout_json TEXT",
```

- [ ] **Step 1.3: Confirm the app still imports cleanly**

Run: `.venv/bin/python -c "from app.main import app; print('ok')"`
Expected: prints `ok`, no tracebacks.

- [ ] **Step 1.4: Commit**

```bash
git add app/models/db.py app/core/db.py
git commit -m "feat(backend): add dashboard_layout_json column to SystemConfig"
```

---

## Task 2: Backend — failing tests for `/dashboard-layout`

**Files:**
- Create: `tests/integration/test_dashboard_layout.py`

- [ ] **Step 2.1: Create the test file**

Create `tests/integration/test_dashboard_layout.py`:

```python
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app


@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_get_dashboard_layout_empty_default(client: TestClient):
    r = client.get("/api/v1/system/dashboard-layout")
    assert r.status_code == 200
    assert r.json() == {"provider_order": [], "card_orders": {}}


def test_put_and_get_roundtrip(client: TestClient):
    body = {
        "provider_order": ["anthropic", "gemini"],
        "card_orders": {"anthropic": ["acc1|Claude Pro||5hr_limit"]},
    }
    r = client.put("/api/v1/system/dashboard-layout", json=body)
    assert r.status_code == 200
    assert r.json() == {"status": "saved"}

    r2 = client.get("/api/v1/system/dashboard-layout")
    assert r2.status_code == 200
    assert r2.json() == body


def test_put_malformed_rejected(client: TestClient):
    # provider_order must be a list of strings
    r = client.put(
        "/api/v1/system/dashboard-layout",
        json={"provider_order": [1, 2], "card_orders": {}},
    )
    assert r.status_code == 422


def test_put_overwrites_previous(client: TestClient):
    client.put(
        "/api/v1/system/dashboard-layout",
        json={"provider_order": ["a"], "card_orders": {}},
    )
    client.put(
        "/api/v1/system/dashboard-layout",
        json={"provider_order": ["b"], "card_orders": {}},
    )
    r = client.get("/api/v1/system/dashboard-layout")
    assert r.json()["provider_order"] == ["b"]
```

- [ ] **Step 2.2: Run to verify the tests fail (routes not implemented yet)**

Run: `.venv/bin/python -m pytest tests/integration/test_dashboard_layout.py -v`
Expected: 4 failures — all four tests return `404 Not Found` because the routes don't exist.

---

## Task 3: Backend — implement `/dashboard-layout` endpoints

**Files:**
- Modify: `app/api/endpoints/system.py`

- [ ] **Step 3.1: Add the Pydantic model and routes**

Edit `app/api/endpoints/system.py`. Add the Pydantic model near the other `_*Update`/`_*Create` classes (e.g. just below `_AppConfigUpdate` around line 333):

```python
class _DashboardLayout(BaseModel):
    provider_order: list[str] = Field(default_factory=list)
    card_orders: dict[str, list[str]] = Field(default_factory=dict)
```

Append two new routes at the bottom of the file:

```python
@router.get("/dashboard-layout")
@limiter.limit("30/minute")
async def get_dashboard_layout(
    request: Request, session: Session = Depends(get_session)
) -> dict:
    """Return the persisted dashboard layout. Empty default if unset."""
    import json

    cfg = session.exec(select(SystemConfig)).first()
    raw = cfg.dashboard_layout_json if cfg else None
    if not raw:
        return {"provider_order": [], "card_orders": {}}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"provider_order": [], "card_orders": {}}
    return {
        "provider_order": parsed.get("provider_order", []) or [],
        "card_orders": parsed.get("card_orders", {}) or {},
    }


@router.put("/dashboard-layout")
@limiter.limit("30/minute")
async def put_dashboard_layout(
    request: Request,
    body: _DashboardLayout,
    session: Session = Depends(get_session),
) -> dict:
    """Store a new dashboard layout. No admin key — matches other UI-facing settings."""
    import json

    cfg = session.exec(select(SystemConfig)).first()
    if cfg is None:
        cfg = SystemConfig()
        session.add(cfg)
    cfg.dashboard_layout_json = json.dumps(body.model_dump())
    session.commit()
    return {"status": "saved"}
```

- [ ] **Step 3.2: Run the new tests and verify they pass**

Run: `.venv/bin/python -m pytest tests/integration/test_dashboard_layout.py -v`
Expected: 4 passed.

- [ ] **Step 3.3: Run the full test suite to confirm nothing regressed**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all pre-existing tests still pass, plus the 4 new ones.

- [ ] **Step 3.4: Commit**

```bash
git add app/api/endpoints/system.py tests/integration/test_dashboard_layout.py
git commit -m "feat(backend): add /api/v1/system/dashboard-layout GET+PUT"
```

---

## Task 4: Frontend — pure layout helpers

**Files:**
- Create: `frontend/js/layout.js`

- [ ] **Step 4.1: Create the module**

Create `frontend/js/layout.js`:

```javascript
/**
 * Pure helpers for applying a user-defined dashboard layout to live card data.
 * No DOM, no network — easy to reason about and to unit-test later.
 */

/**
 * Stable identity of a card within its provider.
 * @param {object} card - LimitCard dict from /api/v1/usage/limits
 * @returns {string} e.g. "acc-xyz|Claude Pro|claude-sonnet|5hr_limit"
 */
export function cardKey(card) {
    const account = card.account_id ?? '';
    const service = card.service_name ?? '';
    const model = card.model_id ?? '';
    const window = card.window_type ?? '';
    return `${account}|${service}|${model}|${window}`;
}

/**
 * Build an ordered list from a mix of pinned and unpinned items.
 * Pinned items appear first, in the order they appear in `orderKeys`.
 * Unpinned items keep their input order (which callers may have pre-sorted).
 * Keys in `orderKeys` that don't exist in `items` are silently dropped.
 *
 * @template T
 * @param {Array<T>} items
 * @param {(item: T) => string} keyOf
 * @param {Array<string>} orderKeys
 * @returns {Array<T>}
 */
export function applyOrder(items, keyOf, orderKeys) {
    const byKey = new Map(items.map(i => [keyOf(i), i]));
    const pinned = [];
    const seen = new Set();
    for (const k of orderKeys) {
        if (byKey.has(k) && !seen.has(k)) {
            pinned.push(byKey.get(k));
            seen.add(k);
        }
    }
    const unpinned = items.filter(i => !seen.has(keyOf(i)));
    return [...pinned, ...unpinned];
}

/**
 * Read the current provider order from the DOM.
 * @param {HTMLElement} gridEl - container with direct children carrying [data-provider-id]
 * @returns {Array<string>}
 */
export function extractProviderOrder(gridEl) {
    if (!gridEl) return [];
    return [...gridEl.querySelectorAll('[data-provider-id]')].map(
        el => el.dataset.providerId
    );
}

/**
 * Read the current card order for a specific container.
 * @param {HTMLElement} containerEl - container with children carrying [data-card-key]
 * @returns {Array<string>}
 */
export function extractCardOrder(containerEl) {
    if (!containerEl) return [];
    return [...containerEl.querySelectorAll('[data-card-key]')].map(
        el => el.dataset.cardKey
    );
}
```

- [ ] **Step 4.2: Commit**

```bash
git add frontend/js/layout.js
git commit -m "feat(frontend): add layout helpers for dashboard reorder"
```

---

## Task 5: Frontend — Sortable.js lazy loader

**Files:**
- Create: `frontend/js/sortable.js`

- [ ] **Step 5.1: Create the loader**

Create `frontend/js/sortable.js`. Mirrors the `ensureChartJS` pattern in `frontend/js/charts.js:74-95`:

```javascript
// Lazy-loads Sortable.js from a CDN on first use.
// Mirrors the ensureChartJS pattern in charts.js.

let _sortable = null;
let _loading = null;

const CDN = 'https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js';

export async function ensureSortable() {
    if (_sortable) return _sortable;
    if (window.Sortable) {
        _sortable = window.Sortable;
        return _sortable;
    }
    if (!_loading) {
        _loading = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = CDN;
            script.onload = () => resolve(window.Sortable);
            script.onerror = () => reject(new Error('Failed to load Sortable.js'));
            document.head.appendChild(script);
        });
    }
    _sortable = await _loading;
    return _sortable;
}
```

- [ ] **Step 5.2: Commit**

```bash
git add frontend/js/sortable.js
git commit -m "feat(frontend): add Sortable.js lazy CDN loader"
```

---

## Task 6: Frontend — layout API client

**Files:**
- Modify: `frontend/js/api.js`

- [ ] **Step 6.1: Add the two functions**

Append to `frontend/js/api.js` (after `putProviderConfig`, around line 200):

```javascript
/**
 * Dashboard Layout
 */

export async function getDashboardLayout() {
    const resp = await fetch('/api/v1/system/dashboard-layout');
    if (!resp.ok) throw new Error(`Failed to fetch dashboard layout: HTTP ${resp.status}`);
    return await resp.json();
}

export async function putDashboardLayout(layout) {
    const resp = await fetch('/api/v1/system/dashboard-layout', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(layout),
    });
    if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${resp.status}`);
    }
    return await resp.json();
}
```

- [ ] **Step 6.2: Commit**

```bash
git add frontend/js/api.js
git commit -m "feat(frontend): add getDashboardLayout + putDashboardLayout"
```

---

## Task 7: Frontend — state additions

**Files:**
- Modify: `frontend/js/state.js`

- [ ] **Step 7.1: Extend STATE**

Replace the `STATE` declaration block in `frontend/js/state.js` (lines 12-30) with:

```javascript
export const STATE = {
    compact: localStorage.getItem('runway_compact') === 'true',
    remaining: localStorage.getItem('runway_remaining') === 'true',
    brightMode: localStorage.getItem('runway_bright_mode') === 'true',
    githubAuth: { authenticated: false, account: null },
    data: [],
    // Dashboard context filter
    activeFilter: (() => {
        const stored = JSON.parse(localStorage.getItem('runway_active_filter') || 'null');
        if (stored && !['account_label', 'sidecar_id', 'window_type'].includes(stored.dimension)) return null;
        return stored;
    })(),
    filterDimension: (() => {
        const stored = localStorage.getItem('runway_filter_dimension');
        return ['account_label', 'sidecar_id', 'window_type'].includes(stored) ? stored : 'account_label';
    })(),
    // Dashboard reordering
    editMode: false,
    layout: (() => {
        try {
            const stored = JSON.parse(localStorage.getItem('runway_layout') || 'null');
            if (stored && Array.isArray(stored.provider_order) && stored.card_orders && typeof stored.card_orders === 'object') {
                return stored;
            }
        } catch {}
        return { provider_order: [], card_orders: {} };
    })(),
};
```

- [ ] **Step 7.2: Smoke check — load the app in the browser**

Run: `.venv/bin/python -m uvicorn app.main:app --reload --port 8765` (leave it running)
Open `http://127.0.0.1:8765/` and confirm the dashboard still renders. No errors in devtools console.

- [ ] **Step 7.3: Commit**

```bash
git add frontend/js/state.js
git commit -m "feat(frontend): add editMode + layout cache to STATE"
```

---

## Task 8: Frontend — header icon rework

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 8.1: Replace the header control block**

In `frontend/index.html`, replace lines 41-48 (the `<div class="flex items-center gap-2">...</div>` block holding the theme + refresh buttons) with:

```html
                <div class="flex items-center gap-1" id="header-controls">
                    <button id="toggle-edit" class="icon-btn" title="Edit layout" aria-pressed="false">
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>
                            <path d="m15 5 4 4"/>
                        </svg>
                    </button>
                    <button id="toggle-theme" class="icon-btn" title="Toggle bright/dark">
                        <svg id="theme-icon-sun" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="12" cy="12" r="4"/>
                            <path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>
                        </svg>
                        <svg id="theme-icon-moon" class="hidden" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>
                        </svg>
                    </button>
                    <button id="refresh-btn" class="icon-btn" title="Force sync all providers">
                        <svg id="refresh-icon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
                            <path d="M3 3v5h5"/>
                            <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/>
                            <path d="M16 16h5v5"/>
                        </svg>
                    </button>
                    <span id="last-updated" class="text-[10px] text-zinc-600 mono ml-2 hidden"></span>
                </div>
```

- [ ] **Step 8.2: Add `.icon-btn` + `.edit-mode` styles**

Append to the `<style>` block at the top of `frontend/index.html` (or the first `<style>` tag in the file — whichever holds the other custom classes). Search the file for `.toggle-btn` to find the right block. Add:

```css
.icon-btn {
    width: 32px;
    height: 32px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 8px;
    border: 1px solid rgba(63, 63, 70, 0.5);
    background: rgba(39, 39, 42, 0.4);
    color: rgb(113, 113, 122);
    transition: color 0.15s, background 0.15s, border-color 0.15s;
}
.icon-btn:hover { color: rgb(228, 228, 231); background: rgba(63, 63, 70, 0.5); }
.icon-btn[aria-pressed="true"] {
    color: rgb(244, 244, 245);
    background: rgb(63, 63, 70);
    border-color: rgb(113, 113, 122);
}

/* Edit mode affordances */
.edit-mode [data-provider-id],
.edit-mode [data-card-key] {
    cursor: grab;
    outline: 1px dashed rgba(113, 113, 122, 0.4);
    outline-offset: 4px;
}
.edit-mode [data-provider-id]:active,
.edit-mode [data-card-key]:active { cursor: grabbing; }
.drag-handle { display: none; }
.edit-mode .drag-handle {
    display: inline-flex;
    color: rgb(113, 113, 122);
    position: absolute;
    top: 8px;
    right: 8px;
}
.sortable-ghost { opacity: 0.4; }
.sortable-chosen { box-shadow: 0 0 0 2px rgba(139, 92, 246, 0.6); }
```

- [ ] **Step 8.3: Update the theme-toggle handler to flip icons instead of emoji**

Edit `frontend/js/app.js` around line 884-888 — replace the existing theme-init block:

```javascript
    // Initialize theme
    applyTheme();
    const sunIcon = document.getElementById('theme-icon-sun');
    const moonIcon = document.getElementById('theme-icon-moon');
    if (sunIcon && moonIcon) {
        sunIcon.classList.toggle('hidden', STATE.brightMode);
        moonIcon.classList.toggle('hidden', !STATE.brightMode);
    }
```

Then find the `toggleTheme` function (search `applyTheme` / `toggleTheme` in `app.js` — it's where `themeBtn.innerHTML` is set) and replace any `themeBtn.innerHTML = ...` / `themeBtn.title = ...` lines with:

```javascript
    const sunIcon = document.getElementById('theme-icon-sun');
    const moonIcon = document.getElementById('theme-icon-moon');
    if (sunIcon && moonIcon) {
        sunIcon.classList.toggle('hidden', STATE.brightMode);
        moonIcon.classList.toggle('hidden', !STATE.brightMode);
    }
```

(Search for `themeBtn.innerHTML` in `app.js` first; there may be one or two occurrences. Replace all.)

- [ ] **Step 8.4: Manual verify**

With `uvicorn` still running, reload the browser. Confirm:
- Three icon-only buttons on the right of the header: pencil, sun (or moon in bright mode), refresh arrows.
- No "SYNC" text label.
- Hovering each button lightens it.
- Clicking the theme toggle swaps sun ↔ moon.
- Clicking refresh still triggers a poll (existing behavior).
- Clicking the pencil does nothing yet (handler wired in Task 10).

- [ ] **Step 8.5: Commit**

```bash
git add frontend/index.html frontend/js/app.js
git commit -m "feat(frontend): unify header controls as icon-only buttons"
```

---

## Task 9: Frontend — tab persistence via URL hash

**Files:**
- Modify: `frontend/js/app.js`

- [ ] **Step 9.1: Update `switchView` to sync the hash**

Edit `frontend/js/app.js` at the `switchView` function (line 35). Replace the function body so the last line of the `window.switchView` definition sets the hash and a new helper reads it on boot:

```javascript
const KNOWN_VIEWS = ['dashboard', 'history', 'fleet', 'settings'];

window.switchView = async function(viewId) {
    if (!KNOWN_VIEWS.includes(viewId)) viewId = 'dashboard';
    // Hide all views
    document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
    document.getElementById(`view-${viewId}`).classList.remove('hidden');

    // Update nav links
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    document.getElementById(`nav-${viewId}`).classList.add('active');

    // Sync URL (no scroll jump) — pushState avoids adding duplicate hashchange events.
    const target = `#${viewId}`;
    if (location.hash !== target) {
        history.replaceState(null, '', target);
    }

    if (viewId === 'dashboard' && STATE.data.length === 0) {
        await loadDashboard();
    }
    if (viewId === 'history') loadHistoryView();
    if (viewId === 'settings') loadSettingsView();
    if (viewId === 'fleet') loadFleetView();
};
```

- [ ] **Step 9.2: Read the hash on boot + listen for hashchange**

Find the `init()`/`DOMContentLoaded` block in `app.js` (around line 880). Replace the default-view call site — currently dashboard is shown by default because `view-dashboard` doesn't have `.hidden`. Add at the end of the init function, just before `initDashboardView()` (line 908):

```javascript
    // Route from URL hash (so reloads stay on the active tab)
    const initialView = (location.hash || '#dashboard').replace(/^#/, '');
    await switchView(initialView);

    window.addEventListener('hashchange', () => {
        const v = (location.hash || '#dashboard').replace(/^#/, '');
        switchView(v);
    });
```

Also — remove the `class="active"` default from `#nav-dashboard` in `frontend/index.html:33` so the initial class state is set entirely by `switchView`:

```html
                    <button class="nav-link" id="nav-dashboard">Dashboard</button>
```

And add `class="view hidden"` to `#view-dashboard` in `frontend/index.html:55` so all views start hidden and `switchView` shows the correct one:

```html
            <section id="view-dashboard" class="view hidden">
```

- [ ] **Step 9.3: Manual verify**

Reload the browser. Confirm:
- Loading `/` → shows Dashboard (default), URL becomes `/#dashboard`.
- Click History → URL becomes `/#history`, history view shows.
- Hard reload → still on History.
- Click the browser back button → returns to Dashboard.
- Manually entering `/#fleet` in the address bar and hitting enter → Fleet view shows.

- [ ] **Step 9.4: Commit**

```bash
git add frontend/js/app.js frontend/index.html
git commit -m "feat(frontend): persist active tab via URL hash"
```

---

## Task 10: Frontend — stamp provider & card identity in DOM

**Files:**
- Modify: `frontend/js/components.js`

- [ ] **Step 10.1: Add `data-provider-id` to provider summary cards**

Find `buildProviderSummaryCard` in `frontend/js/components.js` (around line 1082). In the outermost returned element, add a `data-provider-id` attribute. Locate the opening tag (a `<div class="glass-panel...">` or similar) and add:

```javascript
// before:  <div class="glass-panel ..." onclick="openProviderModal('${providerId}')">
// after:   <div class="glass-panel ..." data-provider-id="${escapeHTMLAttr(providerId)}" onclick="openProviderModal('${providerId}')">
```

Also append the drag-handle grip (visible only in `.edit-mode`) just inside the opening div, before the existing content:

```html
<span class="drag-handle" aria-hidden="true" onclick="event.stopPropagation()">
  <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
    <circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/>
    <circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/>
    <circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/>
  </svg>
</span>
```

(The `event.stopPropagation()` prevents the grip tap from firing the modal's click handler in edit mode.)

- [ ] **Step 10.2: Add `data-provider-id` to modal provider sections**

Find `buildProviderSection` in `frontend/js/components.js` (around line 45). Change the outer wrapper from:

```html
<div class="provider-section mb-8">
```

to:

```html
<div class="provider-section mb-8" data-provider-id="${escapeHTMLAttr(providerId)}">
```

The inner `<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">` is the container that Task 12 will attach Sortable to — no change needed there.

- [ ] **Step 10.3: Add `data-card-key` to individual cards**

Find `buildCard` in `frontend/js/components.js` (around line 450). Add an `import { cardKey } from './layout.js';` at the top of the file (if not present).

In the rendered card root element (compact mode at ~line 493 and standard mode at ~line 579), add:

```javascript
data-card-key="${escapeHTMLAttr(cardKey(item))}"
```

to the same element that already has `data-service="${...}"`.

Also add the same grip glyph (identical SVG as in Step 10.1) near the top-right of each card's root element.

- [ ] **Step 10.4: Manual verify**

Reload the browser. Open devtools, inspect the dashboard grid: each provider card should carry `data-provider-id="..."`. Open a provider modal: each `.provider-section` should carry `data-provider-id="..."` and each card inside should carry `data-card-key="..."`.

- [ ] **Step 10.5: Commit**

```bash
git add frontend/js/components.js
git commit -m "feat(frontend): stamp data-provider-id/data-card-key + grip glyph"
```

---

## Task 11: Frontend — apply saved layout in `renderGrid`

**Files:**
- Modify: `frontend/js/app.js`

- [ ] **Step 11.1: Import helpers**

Add to the imports at the top of `frontend/js/app.js`:

```javascript
import { applyOrder, cardKey } from './layout.js';
import { getDashboardLayout, putDashboardLayout } from './api.js';
```

- [ ] **Step 11.2: Fetch layout on boot, cache in STATE**

In the `init()` function (around line 880), before the `switchView` call added in Task 9, add:

```javascript
    try {
        const layout = await getDashboardLayout();
        STATE.layout = layout;
        localStorage.setItem('runway_layout', JSON.stringify(layout));
    } catch (err) {
        console.warn('Failed to fetch dashboard layout; using cached/empty', err);
    }
```

- [ ] **Step 11.3: Apply order in `renderGrid`**

Edit `renderGrid` in `app.js` (line 696). Replace the provider sort block (lines 709-718) with:

```javascript
    // Default sort: providers with worst health first, then alphabetically
    const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };
    const defaultSorted = [...groups.keys()].sort((a, b) => {
        if (a === '__other__') return 1;
        if (b === '__other__') return -1;
        const aWorst = groups.get(a).reduce((m, i) => Math.max(m, HEALTH_SEVERITY[i.health] || 0), 0);
        const bWorst = groups.get(b).reduce((m, i) => Math.max(m, HEALTH_SEVERITY[i.health] || 0), 0);
        if (bWorst !== aWorst) return bWorst - aWorst;
        return a.localeCompare(b);
    });

    // Apply user-defined provider order on top of the default sort
    const sorted = applyOrder(
        defaultSorted.map(pid => ({ pid })),
        x => x.pid,
        STATE.layout?.provider_order ?? []
    ).map(x => x.pid);
```

- [ ] **Step 11.4: Do the same for per-provider card order inside the modal**

Find `openProviderModal` (around line 745 in `app.js`). Where it filters the items for that provider (line 746), wrap the filtered list with `applyOrder` using `cardKey` and the saved per-provider order:

```javascript
    let items = STATE.data.filter(d => (d.provider_id || '__other__') === providerId);
    items = applyOrder(items, cardKey, STATE.layout?.card_orders?.[providerId] ?? []);
```

- [ ] **Step 11.5: Also apply to `frontend/js/views/dashboard.js`**

Open `frontend/js/views/dashboard.js`. Find its near-identical grouping/sort block (the `renderGrid` in that file). Apply the same two-line change: import `applyOrder`, then after the default sort, wrap with `applyOrder(..., STATE.layout?.provider_order ?? [])`.

If the file doesn't yet import `STATE`, add `import { STATE } from '../state.js';` at the top.

- [ ] **Step 11.6: Manual verify**

Reload. No layout saved yet → dashboard looks identical to before. Then manually seed a layout:

```bash
curl -X PUT http://127.0.0.1:8765/api/v1/system/dashboard-layout \
  -H 'Content-Type: application/json' \
  -d '{"provider_order":["gemini","anthropic"],"card_orders":{}}'
```

Reload — Gemini card should appear before Anthropic regardless of health. Other providers still appear after, in health/alphabetical order. No console errors.

Clear it for the next task:

```bash
curl -X PUT http://127.0.0.1:8765/api/v1/system/dashboard-layout \
  -H 'Content-Type: application/json' -d '{"provider_order":[],"card_orders":{}}'
```

- [ ] **Step 11.7: Commit**

```bash
git add frontend/js/app.js frontend/js/views/dashboard.js
git commit -m "feat(frontend): render dashboard with saved layout applied"
```

---

## Task 12: Frontend — edit mode toggle + Sortable wiring

**Files:**
- Modify: `frontend/js/app.js`

- [ ] **Step 12.1: Add imports**

Add to the top of `frontend/js/app.js`:

```javascript
import { ensureSortable } from './sortable.js';
import { extractProviderOrder, extractCardOrder } from './layout.js';
```

- [ ] **Step 12.2: Add toggle + Sortable wiring**

Append these functions to `frontend/js/app.js` (near other top-level helpers like `toggleTheme`):

```javascript
let _providerSortable = null;
let _cardSortables = [];

async function enterEditMode() {
    STATE.editMode = true;
    document.body.classList.add('edit-mode');
    const btn = document.getElementById('toggle-edit');
    if (btn) { btn.setAttribute('aria-pressed', 'true'); btn.title = 'Done'; }

    const Sortable = await ensureSortable();

    // Provider grid (outer wrapper injected by renderGrid)
    const providerGrid = document.querySelector('#grid > div');
    if (providerGrid) {
        _providerSortable = new Sortable(providerGrid, {
            animation: 150,
            draggable: '[data-provider-id]',
            handle: '.drag-handle',
            onEnd: onProviderDrop,
        });
    }

    // Card grids inside any currently-open modal
    document.querySelectorAll('#modal-content [data-provider-id]').forEach(section => {
        const container = section.querySelector('.card-grid, .grid') || section;
        const s = new Sortable(container, {
            animation: 150,
            draggable: '[data-card-key]',
            handle: '.drag-handle',
            onEnd: () => onCardDrop(section.dataset.providerId, container),
        });
        _cardSortables.push(s);
    });
}

function exitEditMode() {
    STATE.editMode = false;
    document.body.classList.remove('edit-mode');
    const btn = document.getElementById('toggle-edit');
    if (btn) { btn.setAttribute('aria-pressed', 'false'); btn.title = 'Edit layout'; }

    if (_providerSortable) { _providerSortable.destroy(); _providerSortable = null; }
    _cardSortables.forEach(s => s.destroy());
    _cardSortables = [];
}

async function onProviderDrop() {
    const providerGrid = document.querySelector('#grid > div');
    const order = extractProviderOrder(providerGrid);
    STATE.layout = { ...STATE.layout, provider_order: order };
    await persistLayout();
}

async function onCardDrop(providerId, container) {
    const order = extractCardOrder(container);
    STATE.layout = {
        ...STATE.layout,
        card_orders: { ...STATE.layout.card_orders, [providerId]: order },
    };
    await persistLayout();
}

async function persistLayout() {
    localStorage.setItem('runway_layout', JSON.stringify(STATE.layout));
    try {
        await putDashboardLayout(STATE.layout);
    } catch (err) {
        console.warn('Failed to persist layout (kept in localStorage)', err);
    }
}
```

- [ ] **Step 12.3: Wire the pencil button**

In the `init()` function in `app.js`, where other header buttons are wired (around line 901-905), add:

```javascript
    document.getElementById('toggle-edit')?.addEventListener('click', () => {
        if (STATE.editMode) exitEditMode();
        else enterEditMode();
    });
```

- [ ] **Step 12.4: Re-init card sortables when a modal opens in edit mode**

Find `openProviderModal` in `app.js` (around line 745). At the very end of the function (after the modal has been rendered), add:

```javascript
    if (STATE.editMode) {
        const Sortable = await ensureSortable();
        document.querySelectorAll('#modal-content [data-provider-id]').forEach(section => {
            const container = section.querySelector('.card-grid, .grid') || section;
            const s = new Sortable(container, {
                animation: 150,
                draggable: '[data-card-key]',
                handle: '.drag-handle',
                onEnd: () => onCardDrop(section.dataset.providerId, container),
            });
            _cardSortables.push(s);
        });
    }
```

- [ ] **Step 12.5: Manual verify end-to-end**

With uvicorn running:
1. Reload browser. Click pencil → pencil becomes filled, dashboard gains dashed outlines, grip glyphs appear on each provider card.
2. Drag Gemini above Anthropic → network tab shows `PUT /api/v1/system/dashboard-layout` with the new order.
3. Click pencil again → edit mode exits, outlines disappear.
4. Hard reload → order persists.
5. Click a provider card → modal opens. Click pencil → card grid inside modal becomes draggable. Drag a card. Close modal. Reopen → order persists.
6. Delete the layout row:
   ```bash
   curl -X PUT http://127.0.0.1:8765/api/v1/system/dashboard-layout \
     -H 'Content-Type: application/json' -d '{"provider_order":[],"card_orders":{}}'
   ```
   Reload → order reverts to default health-based sort.

- [ ] **Step 12.6: Commit**

```bash
git add frontend/js/app.js
git commit -m "feat(frontend): edit-mode toggle + drag-to-reorder via Sortable.js"
```

---

## Task 13: Final self-check

- [ ] **Step 13.1: Run the backend test suite one last time**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all green.

- [ ] **Step 13.2: Manual regression pass**

With uvicorn running and the browser open:
1. Reorder providers → reload → order persists. ✔
2. Reorder cards inside a provider modal → reload → order persists. ✔
3. Tab switches update URL hash; hard reload stays on tab. ✔
4. Header shows three grayscale icon-only buttons with hover feedback. ✔
5. Theme toggle still swaps light/dark. ✔
6. Refresh button still triggers a poll. ✔
7. When a provider is disabled/removed, remaining cards keep their saved order. ✔
8. Empty layout → default health-based sort applies. ✔

- [ ] **Step 13.3: Update spec if anything drifted during implementation**

If the implementation diverged from the spec in any way (e.g. a class name changed, an endpoint path changed), edit `docs/superpowers/specs/2026-04-14-dashboard-reorder-and-header-polish-design.md` to reflect reality, and commit:

```bash
git add docs/superpowers/specs/2026-04-14-dashboard-reorder-and-header-polish-design.md
git commit -m "docs(spec): sync dashboard-reorder spec with implementation"
```

---

## Notes & Deliberate Non-Features

- **No admin key** on layout endpoints. Matches existing UI-facing settings; this is a single-user app.
- **No cross-section card drag.** Cards cannot move between providers in v1. Easy to add later by extending Sortable's `group` option.
- **No keyboard reordering.** Drag-only. Follow-up work can add `↑/↓` buttons for a11y.
- **No separate Save button.** Every drop persists.
- **Layout stored as a JSON string**, not a related table. Keeps the migration additive and the blob is tiny.
- **Fallback behavior on fetch failure:** the app uses the localStorage-cached layout and logs a warning. PUT failures log, localStorage still reflects the intended state, and the next successful PUT syncs.
