# Dashboard & History UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the health bar, provider aggregate cards, provider drill-down modal, history sparkline strip, and history chart controls from the UI redesign spec.

**Architecture:** Pure frontend changes. New component functions in `components.js` build HTML strings (existing pattern). `renderGrid()` in `app.js` switches from `buildProviderSection()` (per-service grid) to `buildProviderSummaryCard()` (one aggregate card per provider). Individual service detail moves into a modal fetched on click. History controls wire to existing `fetchHistory(params)` which already accepts `days` and `provider_id`.

**Tech Stack:** Vanilla JS ES modules, Tailwind CSS v4, Chart.js 4.4 (CDN), inline SVG for sparklines. No new dependencies.

> **Scope note:** Settings tab + Provider Config backend is a separate plan (Plan 2). This plan has zero backend changes.

---

## File Map

| File | Change |
|---|---|
| `frontend/js/components.js` | Add `buildHealthBar`, `buildProviderSummaryCard`, `buildSparklineSVG` (internal), `buildProviderModal`, `buildProviderSparklineStrip` |
| `frontend/js/app.js` | Update `renderGrid`, add `renderHealthBar`, add `window.openProviderModal`, update `loadHistory` with state for filters |
| `frontend/js/charts.js` | Add `metric` param to `updateCharts`, update `bucketByDay` to support `percent` / `tokens` / `cost` modes |
| `frontend/index.html` | Add `#health-bar` container; add sparkline strip + chart controls to History view |

---

## Task 1: Health Bar Component

**Files:**
- Modify: `frontend/js/components.js` (add export after existing exports)
- Modify: `frontend/index.html` (add container div)
- Modify: `frontend/js/app.js` (add `renderHealthBar`, call from `loadData`)

### Step 1 — Add `buildHealthBar` to `components.js`

Add this function at the bottom of `frontend/js/components.js`, before the final blank line:

```javascript
/**
 * Build the top health overview bar (4 stat tiles + proportional bar).
 * @param {Array} data - Full array of LimitCard items from STATE.data
 * @returns {string} HTML string
 */
export function buildHealthBar(data) {
    const counts = { critical: 0, warning: 0, good: 0, unlimited: 0 };
    for (const item of data) {
        if (counts[item.health] !== undefined) counts[item.health]++;
    }
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    if (total === 0) return '';

    const tiles = [
        { key: 'critical',  label: 'Critical',  textColor: 'text-red-400',    border: 'border-red-900/50',    bg: 'bg-red-950/20'    },
        { key: 'warning',   label: 'Warning',   textColor: 'text-amber-400',  border: 'border-amber-900/50', bg: 'bg-amber-950/20'  },
        { key: 'good',      label: 'Good',      textColor: 'text-green-400',  border: 'border-green-900/50', bg: 'bg-green-950/20'  },
        { key: 'unlimited', label: 'Unlimited', textColor: 'text-violet-400', border: 'border-violet-900/50',bg: 'bg-violet-950/20' },
    ];
    const BAR_HEX = { critical: '#ef4444', warning: '#eab308', good: '#22c55e', unlimited: '#8b5cf6' };

    const tilesHTML = tiles.map(t => `
        <div class="glass-panel ${t.border} ${t.bg} rounded-xl p-3 text-center${counts[t.key] === 0 ? ' opacity-30' : ''}">
            <div class="text-3xl font-black ${t.textColor} leading-none">${counts[t.key]}</div>
            <div class="text-[9px] text-zinc-500 uppercase tracking-widest mt-1.5">${t.label}</div>
        </div>`).join('');

    const barSegments = tiles
        .filter(t => counts[t.key] > 0)
        .map(t => `<div style="flex:${counts[t.key]};background:${BAR_HEX[t.key]};border-radius:2px;"></div>`)
        .join('');

    return `<div class="mb-6">
        <div class="grid grid-cols-4 gap-3 mb-2">${tilesHTML}</div>
        <div class="h-1.5 rounded-full overflow-hidden flex gap-0.5">${barSegments}</div>
    </div>`;
}
```

### Step 2 — Add health bar container to `index.html`

In `frontend/index.html`, inside `<section id="view-dashboard" class="view">`, add a container **before** the `<!-- Loading skeletons -->` comment:

```html
<!-- Health bar -->
<div id="health-bar"></div>
```

Result (excerpt):
```html
<section id="view-dashboard" class="view">
    <!-- Health bar -->
    <div id="health-bar"></div>

    <!-- Loading skeletons -->
    <div id="loading" ...>
```

### Step 3 — Add `renderHealthBar` to `app.js` and call it from `loadData`

Add this function in `frontend/js/app.js`, just above `renderGrid`:

```javascript
function renderHealthBar() {
    const el = document.getElementById('health-bar');
    if (!el) return;
    el.innerHTML = buildHealthBar(STATE.data);
}
```

Update the import at line 3 of `app.js` to include `buildHealthBar`:

```javascript
import { buildCard, buildModalContent, buildGitHubOAuthModal, buildProviderSection, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildHealthBar } from './components.js';
```

In `loadData()`, call `renderHealthBar()` right after `renderGrid()`:

```javascript
STATE.data = json.limits;
renderFilterPills();
renderGrid();
renderHealthBar();  // ← add this line
```

### Step 4 — Verify in browser

Run `make dev`, open `http://localhost:8765`. The Dashboard should show 4 stat tiles (Critical / Warning / Good / Unlimited) with a proportional colored bar below them, above the provider sections. Tiles with zero count are dimmed.

### Step 5 — Commit

```bash
git add frontend/js/components.js frontend/js/app.js frontend/index.html
git commit -m "feat(ui): add health overview bar to dashboard"
```

---

## Task 2: Provider Summary Card Component

**Files:**
- Modify: `frontend/js/components.js` (add `buildProviderSummaryCard` export)

### Step 1 — Add `buildProviderSummaryCard` to `components.js`

Add this function to `frontend/js/components.js` after `buildHealthBar`:

```javascript
/** Health severity for sorting (higher = worse) */
const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };

/**
 * Build a two-zone aggregate provider card for the dashboard grid.
 * Top zone: worst metric, tier, account. Bottom zone: per-service breakdown.
 * @param {string} providerId
 * @param {Array} items - LimitCard items for this provider
 * @returns {string} HTML string
 */
export function buildProviderSummaryCard(providerId, items) {
    if (!items || items.length === 0) return '';

    const icon = PROVIDER_ICONS[providerId] || '🔧';

    // Sort by health severity descending (worst first)
    const sorted = [...items].sort((a, b) =>
        (HEALTH_SEVERITY[b.health] || 0) - (HEALTH_SEVERITY[a.health] || 0)
    );
    const worst = sorted[0];
    const h = HEALTH_CONFIG[worst.health] || HEALTH_CONFIG.unknown;

    // Worst metric display
    let worstPct = null;
    if (!worst.is_unlimited && worst.used_value != null && worst.limit_value > 0) {
        worstPct = (worst.used_value / worst.limit_value) * 100;
    }
    const worstDisplay = worst.is_unlimited
        ? '∞'
        : worstPct != null
        ? `${worstPct.toFixed(1)}%`
        : escapeHTML(worst.remaining || '—');

    const worstColor = worst.is_unlimited ? 'text-violet-400' : h.badge || 'text-zinc-50';

    // Tier badge (first item's tier — same account = same tier)
    const tier = items[0]?.tier;
    const tierBadgeHTML = tier ? getTierBadge(tier) : '';

    // Account labels
    const accounts = [...new Set(items.map(i => i.account_label).filter(Boolean))];
    const accountHTML = accounts.length === 1
        ? `<div class="text-[9px] text-zinc-500 mt-0.5">${escapeHTML(accounts[0])}</div>`
        : accounts.length > 1
        ? `<div class="flex flex-wrap gap-1 mt-1">${accounts.map(a =>
            `<span class="text-[8px] bg-zinc-800 text-zinc-500 px-1.5 py-0.5 rounded">${escapeHTML(a)}</span>`
          ).join('')}</div>`
        : '';

    // Health badge (CRIT / WARN / GOOD / UNLM)
    const badgeLabels = { critical: 'CRIT', warning: 'WARN', good: 'GOOD', unlimited: 'UNLM', unknown: '——' };
    const healthBadgeHTML = `<span class="text-[9px] font-bold px-1.5 py-0.5 rounded border ${h.badge} border-current/30">${badgeLabels[worst.health] || '——'}</span>`;

    // Segmented bar (per-service health counts)
    const barCounts = { critical: 0, warning: 0, good: 0, unlimited: 0 };
    items.forEach(i => { if (barCounts[i.health] !== undefined) barCounts[i.health]++; });
    const BAR_HEX = { critical: '#ef4444', warning: '#eab308', good: '#22c55e', unlimited: '#8b5cf6' };
    const barSegments = Object.entries(barCounts)
        .filter(([, c]) => c > 0)
        .map(([k, c]) => `<div style="flex:${c};background:${BAR_HEX[k]};border-radius:2px;"></div>`)
        .join('');

    // Per-service breakdown rows
    const breakdownRows = sorted.map(item => {
        const dot = HEALTH_CONFIG[item.health]?.dot || 'dot-unknown';
        let pct = null;
        if (!item.is_unlimited && item.used_value != null && item.limit_value > 0) {
            pct = (item.used_value / item.limit_value) * 100;
        }
        const display = item.is_unlimited ? '∞' : pct != null ? `${pct.toFixed(0)}%` : escapeHTML(item.remaining || '—');
        const rowTier = item.tier ? `<span class="text-[7px] font-bold px-1 py-px rounded border ${getTierTextClass(item.tier)} border-current/30 mr-0.5">${escapeHTML(item.tier.toUpperCase().slice(0,3))}</span>` : '';
        return `<div class="flex justify-between items-center text-[9px]">
            <span class="flex items-center gap-1.5 min-w-0">
                <span class="dot ${dot} flex-shrink-0" style="width:6px;height:6px;"></span>
                <span class="text-zinc-300 truncate">${escapeHTML(item.service_name)}</span>
            </span>
            <span class="flex items-center gap-1 text-zinc-500 flex-shrink-0 ml-2">${rowTier}${display}</span>
        </div>`;
    }).join('');

    return `<div class="glass-panel ${h.card} rounded-2xl overflow-hidden cursor-pointer select-none hover:scale-[1.01] active:scale-[0.99] transition-all duration-200"
         onclick="openProviderModal('${escapeHTMLAttr(providerId)}')">
        <div class="p-4">
            <div class="flex justify-between items-start mb-1">
                <div class="min-w-0 flex-1">
                    <div class="flex items-center gap-2 flex-wrap">
                        <span class="text-[11px] font-bold text-zinc-400 uppercase tracking-widest">${icon} ${escapeHTML(providerId)}</span>
                        ${tierBadgeHTML}
                    </div>
                    ${accountHTML}
                </div>
                <div class="ml-2 flex-shrink-0">${healthBadgeHTML}</div>
            </div>
            <div class="text-3xl font-black ${worstColor} leading-none mt-2">${worstDisplay}</div>
            <div class="text-[9px] text-zinc-500 mt-1">${escapeHTML(worst.service_name)} · worst</div>
        </div>
        <div class="border-t border-zinc-800/50 bg-black/20 px-4 py-3">
            <div class="h-0.5 rounded-full overflow-hidden flex gap-0.5 mb-2.5">${barSegments}</div>
            <div class="space-y-1.5">${breakdownRows}</div>
        </div>
    </div>`;
}

/** Return Tailwind text color class for a tier name (used in breakdown rows) */
function getTierTextClass(tier) {
    if (!tier) return 'text-zinc-500';
    const t = tier.toLowerCase();
    if (t.includes('pro') || t.includes('premium') || t.includes('plus')) return 'text-amber-400';
    if (t.includes('team') || t.includes('enterprise')) return 'text-violet-400';
    return 'text-zinc-500';
}
```

### Step 2 — Verify the function exports without errors

Run: `make dev`

Open browser dev tools console. No import errors should appear. The function isn't wired yet so nothing visual changes.

### Step 3 — Commit

```bash
git add frontend/js/components.js
git commit -m "feat(ui): add buildProviderSummaryCard component"
```

---

## Task 3: Wire Provider Cards into the Dashboard Grid

**Files:**
- Modify: `frontend/js/app.js` (update `renderGrid`, add `openProviderModal` stub)
- Modify: `frontend/js/components.js` (add `buildProviderSummaryCard` to import used in app.js)

### Step 1 — Update the import in `app.js`

Replace the existing import line in `frontend/js/app.js`:

```javascript
// Before:
import { buildCard, buildModalContent, buildGitHubOAuthModal, buildProviderSection, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildHealthBar } from './components.js';

// After:
import { buildCard, buildModalContent, buildGitHubOAuthModal, buildProviderSection, buildProviderSummaryCard, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildHealthBar } from './components.js';
```

### Step 2 — Update `renderGrid` to use `buildProviderSummaryCard`

Replace the `renderGrid` function body in `frontend/js/app.js`:

```javascript
function renderGrid() {
    const grid = document.getElementById('grid');

    const visible = applyFilters(STATE.data);

    // Group by provider_id; cards without a provider_id go to '__other__'
    const groups = new Map();
    visible.forEach(item => {
        const key = item.provider_id || '__other__';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(item);
    });

    // Sort: providers with worst health first, then alphabetically
    const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };
    const sorted = [...groups.keys()].sort((a, b) => {
        if (a === '__other__') return 1;
        if (b === '__other__') return -1;
        const aWorst = Math.max(...(groups.get(a).map(i => HEALTH_SEVERITY[i.health] || 0)));
        const bWorst = Math.max(...(groups.get(b).map(i => HEALTH_SEVERITY[i.health] || 0)));
        if (bWorst !== aWorst) return bWorst - aWorst;
        return a.localeCompare(b);
    });

    let html = '';
    let count = 0;
    for (const key of sorted) {
        const items = groups.get(key);
        try {
            html += buildProviderSummaryCard(key, items);
            count += items.length;
        } catch (e) {
            console.error('Failed to render provider card:', key, e);
        }
    }

    if (!html) {
        html = '<p class="text-zinc-500 text-sm text-center py-8">No cards match active filters.</p>';
    }

    // Provider cards use a responsive grid (not provider sections)
    grid.innerHTML = `<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">${html}</div>`;
    document.getElementById('footer-count').textContent = count;
}
```

### Step 3 — Add `openProviderModal` stub to `app.js`

Add this stub after `renderGrid` (it will be fleshed out in Task 4):

```javascript
/**
 * Open the provider drill-down modal. Fetches 7d history for sparklines.
 * @param {string} providerId
 */
window.openProviderModal = async function(providerId) {
    const items = STATE.data.filter(d => (d.provider_id || '__other__') === providerId);
    if (!items.length) return;

    const container = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');

    // Show loading state immediately
    content.innerHTML = `<div class="p-8 text-center text-zinc-500 animate-pulse">Loading ${providerId}…</div>`;
    container.classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('modal-backdrop').onclick = closeModal;

    // Placeholder — full implementation in Task 4
    content.innerHTML = `<div class="p-6">
        <div class="flex justify-between items-center mb-4">
            <h2 class="text-lg font-bold text-zinc-100">${providerId}</h2>
            <button id="close-modal" onclick="closeModal()" class="text-zinc-500 hover:text-zinc-300">✕</button>
        </div>
        <p class="text-zinc-500 text-sm">${items.length} service(s) — full modal in Task 4</p>
    </div>`;
    document.getElementById('close-modal').onclick = closeModal;
};
```

### Step 4 — Verify in browser

Run `make dev`, open `http://localhost:8765`. The Dashboard should show:
- Health bar at the top
- One aggregate card per provider (sorted worst-first)
- Each card shows worst %, tier badge, account email, and per-service breakdown
- Clicking a card opens a basic placeholder modal (Task 4 will complete it)

### Step 5 — Commit

```bash
git add frontend/js/app.js
git commit -m "feat(ui): wire provider summary cards into dashboard grid"
```

---

## Task 4: Provider Drill-down Modal with Sparklines

**Files:**
- Modify: `frontend/js/components.js` (add `buildSparklineSVG` internal, `buildProviderModal` export)
- Modify: `frontend/js/app.js` (replace `openProviderModal` stub with full implementation)

### Step 1 — Add sparkline helper and modal builder to `components.js`

Add these functions to `frontend/js/components.js` after `buildProviderSummaryCard`:

```javascript
/**
 * Build a minimal SVG sparkline from a series of {value} points.
 * @param {Array<{value: number}>} points - ordered oldest→newest
 * @param {string} color - hex color
 * @param {number} [width=64]
 * @param {number} [height=28]
 * @returns {string} SVG HTML string
 */
function buildSparklineSVG(points, color, width = 64, height = 28) {
    if (!points || points.length < 2) {
        return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"></svg>`;
    }
    const values = points.map(p => p.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const pad = 3;

    const svgPoints = points.map((p, i) => {
        const x = ((i / (points.length - 1)) * (width - pad * 2) + pad).toFixed(1);
        const y = (height - pad - ((p.value - min) / range) * (height - pad * 2)).toFixed(1);
        return `${x},${y}`;
    }).join(' ');

    const [lastX, lastY] = svgPoints.split(' ').pop().split(',');

    return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" style="overflow:visible;">
        <polyline points="${svgPoints}" fill="none" stroke="${color}" stroke-width="1.5"
            stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${lastX}" cy="${lastY}" r="2.5" fill="${color}"/>
    </svg>`;
}

/**
 * Derive trend arrow from a points series.
 * @param {Array<{value: number}>} points
 * @returns {'↑'|'↓'|'→'}
 */
function getTrendArrow(points) {
    if (!points || points.length < 2) return '→';
    const delta = points[points.length - 1].value - points[0].value;
    if (delta > 3) return '↑';
    if (delta < -3) return '↓';
    return '→';
}

/**
 * Build the provider drill-down modal.
 * @param {string} providerId
 * @param {Array} items - LimitCard items for this provider (sorted worst-first)
 * @param {Array} history - raw history snapshots from /api/v1/usage/history
 * @returns {string} HTML string
 */
export function buildProviderModal(providerId, items, history) {
    const icon = PROVIDER_ICONS[providerId] || '🔧';
    const accounts = [...new Set(items.map(i => i.account_label).filter(Boolean))];
    const accountText = accounts.join(' · ') || '';
    const windowType = items[0]?.window_type || '';
    const serviceCount = items.length;

    const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };
    const sorted = [...items].sort((a, b) =>
        (HEALTH_SEVERITY[b.health] || 0) - (HEALTH_SEVERITY[a.health] || 0)
    );

    const BAR_HEX = { critical: '#ef4444', warning: '#eab308', good: '#22c55e', unlimited: '#8b5cf6', unknown: '#3f3f46' };
    const SOURCE_LABELS = { oauth: 'OAuth', web_api: 'Web API', local: 'Local', cache: 'Cache', fallback: 'Fallback', api: 'API', sidecar: 'Sidecar' };

    const serviceRows = sorted.map(item => {
        const h = HEALTH_CONFIG[item.health] || HEALTH_CONFIG.unknown;
        const barColor = BAR_HEX[item.health] || '#3f3f46';
        const badgeLabels = { critical: 'CRIT', warning: 'WARN', good: 'GOOD', unlimited: 'UNLM', unknown: '——' };

        // Percentage
        let pct = null;
        if (!item.is_unlimited && item.used_value != null && item.limit_value > 0) {
            pct = (item.used_value / item.limit_value) * 100;
        }
        const barWidth = item.is_unlimited ? 100 : (pct ?? 0);

        // Sparkline — filter history for this service
        const svcHistory = (history || [])
            .filter(s => s.provider_id === providerId && s.service_name === item.service_name && s.used_value != null)
            .sort((a, b) => a.timestamp.localeCompare(b.timestamp))
            .map(s => ({ value: s.used_value }));
        const sparkColor = item.is_unlimited ? '#8b5cf6' : barColor;
        const sparkSVG = buildSparklineSVG(svcHistory, sparkColor);
        const trendArrow = getTrendArrow(svcHistory);

        // Used / limit display
        const fmt = formatUsageValues(item.used_value, item.limit_value, item.unit_type, item.currency);
        const usageText = item.is_unlimited
            ? 'Unlimited'
            : (fmt.used !== '—' && fmt.limit !== '—')
            ? `${fmt.used} / ${fmt.limit}${fmt.unit ? ' ' + fmt.unit : ''}`
            : item.remaining || '—';

        const resetText = item.reset_at ? formatResetDisplay(item.reset_at) : (item.reset || '—');
        const sourceLabel = SOURCE_LABELS[item.data_source] || item.data_source || '';
        const paceIcon = getPaceIcon(item.pace);
        const tierBadge = item.tier ? getTierBadge(item.tier) : '';

        return `<div class="bg-zinc-950 border ${h.card.replace('health-', 'border-')} rounded-xl p-3">
            <div class="flex justify-between items-start mb-2">
                <div class="flex-1 min-w-0">
                    <div class="text-[11px] font-bold text-zinc-100">${escapeHTML(item.service_name)}</div>
                    <div class="flex flex-wrap items-center gap-1.5 mt-1">
                        <span class="text-[8px] font-bold px-1.5 py-px rounded border ${h.badge} border-current/30">${badgeLabels[item.health] || '——'}</span>
                        ${tierBadge}
                        ${sourceLabel ? `<span class="text-[8px] text-zinc-600">${escapeHTML(sourceLabel)}</span>` : ''}
                        ${paceIcon ? `<span class="text-[10px]">${paceIcon}</span>` : ''}
                        ${item.pace ? `<span class="text-[8px] text-zinc-600">${escapeHTML(item.pace)}</span>` : ''}
                    </div>
                </div>
                <div class="flex items-center gap-1.5 flex-shrink-0 ml-2">
                    <span class="text-[10px] text-zinc-500">${trendArrow}</span>
                    ${sparkSVG}
                </div>
            </div>
            <div class="flex justify-between text-[9px] text-zinc-500 mb-1.5">
                <span>${escapeHTML(usageText)}</span>
                <span>${escapeHTML(resetText)}</span>
            </div>
            <div class="h-0.5 bg-zinc-800 rounded-full overflow-hidden">
                <div class="h-full rounded-full transition-all" style="width:${Math.min(barWidth, 100).toFixed(1)}%;background:${barColor};"></div>
            </div>
        </div>`;
    }).join('');

    return `<div>
        <div class="flex justify-between items-start mb-4 pb-3 border-b border-zinc-800/50">
            <div>
                <div class="text-base font-black text-zinc-100">${icon} ${escapeHTML(providerId)}</div>
                <div class="text-[10px] text-zinc-500 mt-0.5">${escapeHTML(accountText)}${windowType ? ' · ' + escapeHTML(windowType) : ''} · ${serviceCount} service${serviceCount !== 1 ? 's' : ''}</div>
            </div>
            <button id="close-modal" class="text-zinc-500 hover:text-zinc-300 transition-colors text-lg leading-none mt-0.5">✕</button>
        </div>
        <div class="space-y-3 max-h-[60vh] overflow-y-auto pr-1">${serviceRows}</div>
    </div>`;
}
```

Note: `buildProviderModal` calls `formatUsageValues`, `formatResetDisplay`, `getPaceIcon`, `getTierBadge` — all already defined earlier in `components.js`.

### Step 2 — Update the import in `app.js`

```javascript
import { buildCard, buildModalContent, buildGitHubOAuthModal, buildProviderSection, buildProviderSummaryCard, buildProviderModal, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildHealthBar } from './components.js';
```

### Step 3 — Replace the `openProviderModal` stub in `app.js`

Replace the stub from Task 3 with the full implementation:

```javascript
window.openProviderModal = async function(providerId) {
    const items = STATE.data.filter(d => (d.provider_id || '__other__') === providerId);
    if (!items.length) return;

    const container = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');

    content.innerHTML = `<div class="p-8 text-center text-zinc-500 text-sm animate-pulse">Loading ${escapeHTML(providerId)}…</div>`;
    container.classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('modal-backdrop').onclick = closeModal;

    let history = [];
    try {
        history = await fetchHistory({ provider_id: providerId, days: 7, limit: 500 });
    } catch (e) {
        console.warn('Could not fetch history for modal sparklines:', e.message);
    }

    // Sort items worst-first (same order as card breakdown)
    const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };
    const sorted = [...items].sort((a, b) =>
        (HEALTH_SEVERITY[b.health] || 0) - (HEALTH_SEVERITY[a.health] || 0)
    );

    content.innerHTML = buildProviderModal(providerId, sorted, history);
    document.getElementById('close-modal').onclick = closeModal;
};
```

Also add `fetchHistory` to the import at the top of `app.js`:

```javascript
import { fetchLimits, getGitHubOAuthStatus, initGitHubOAuth, pollGitHubOAuth, logoutGitHub, fetchHistory, fetchSettings, fetchFleet, patchSidecar, deleteSidecarAPI, fetchTokenHealth, postTokenRefresh } from './api.js';
```

(`fetchHistory` was already imported — verify it's present, no change needed if so.)

### Step 4 — Verify in browser

Click any provider card. The modal should open showing:
- Provider icon + name + account + service count in header
- One row per service, ordered worst-first
- Each row: service name, health/tier/source/pace badges, sparkline SVG + trend arrow, used/limit text + reset time, progress bar
- Close button (✕) and backdrop click both close the modal

### Step 5 — Commit

```bash
git add frontend/js/components.js frontend/js/app.js
git commit -m "feat(ui): add provider drill-down modal with sparklines"
```

---

## Task 5: History Sparkline Strip

**Files:**
- Modify: `frontend/js/components.js` (add `buildProviderSparklineStrip` export)
- Modify: `frontend/index.html` (add sparkline strip container in History view)
- Modify: `frontend/js/app.js` (update `loadHistory` to render strip, add click-to-filter)

### Step 1 — Add `buildProviderSparklineStrip` to `components.js`

Add after `buildProviderModal`:

```javascript
/**
 * Build the per-provider sparkline summary strip for the History tab.
 * @param {Array} history - raw history snapshots
 * @param {Set|null} activeProviders - Set of active provider IDs (null = all active)
 * @returns {string} HTML string
 */
export function buildProviderSparklineStrip(history, activeProviders) {
    if (!history || history.length === 0) return '';

    // Group history by provider
    const byProvider = new Map();
    for (const s of history) {
        if (s.used_value == null) continue;
        const pid = s.provider_id || 'unknown';
        if (!byProvider.has(pid)) byProvider.set(pid, []);
        byProvider.get(pid).push(s);
    }

    if (byProvider.size === 0) return '';

    const PROVIDER_HEX = {
        anthropic: '#f59e0b', gemini: '#3b82f6', github: '#8b5cf6',
        chatgpt: '#10b981', opencode: '#06b6d4', openrouter: '#ec4899',
        minimax: '#14b8a6', ollama: '#94a3b8',
    };

    const cards = [...byProvider.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([pid, snaps]) => {
        const icon = PROVIDER_ICONS[pid] || '🔧';
        const color = PROVIDER_HEX[pid] || '#64748b';
        const isActive = !activeProviders || activeProviders.has(pid);

        // Build 7d daily average series
        const byDay = new Map();
        for (const s of snaps) {
            const day = s.timestamp.slice(0, 10);
            if (!byDay.has(day)) byDay.set(day, { sum: 0, count: 0 });
            const b = byDay.get(day);
            b.sum += s.used_value;
            b.count += 1;
        }
        const points = [...byDay.entries()]
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([, b]) => ({ value: b.sum / b.count }));

        const sparkSVG = buildSparklineSVG(points, color, 56, 24);
        const trendArrow = getTrendArrow(points);
        const latestValue = points.length > 0 ? points[points.length - 1].value.toFixed(0) : '—';
        const trendColor = trendArrow === '↑' ? 'text-red-400' : trendArrow === '↓' ? 'text-green-400' : 'text-zinc-400';

        const activeBorder = isActive ? `border-[${color}]` : 'border-zinc-800';
        const activeOpacity = isActive ? '' : 'opacity-40';

        return `<div class="glass-panel border rounded-xl p-3 cursor-pointer select-none hover:opacity-90 transition-all ${activeOpacity}"
                     style="border-color:${isActive ? color : '#27272a'};"
                     onclick="toggleHistoryProvider('${escapeHTMLAttr(pid)}')">
            <div class="flex items-center justify-between gap-2 mb-1.5">
                <span class="text-[9px] font-bold text-zinc-400 uppercase tracking-wide">${icon} ${escapeHTML(pid)}</span>
                ${sparkSVG}
            </div>
            <div class="flex items-baseline gap-1">
                <span class="text-sm font-black text-zinc-100">${latestValue}</span>
                <span class="text-[10px] ${trendColor} font-bold">${trendArrow}</span>
            </div>
        </div>`;
    }).join('');

    return `<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-4">${cards}</div>`;
}
```

### Step 2 — Add sparkline strip container to `index.html`

In the `#view-history` section of `frontend/index.html`, add the strip container **inside** `#history-chart-panel`, **above** the existing chart:

```html
<div id="history-chart-panel" class="glass-panel rounded-3xl p-6 mb-6">
    <div class="flex items-center justify-between mb-4">
        <h3 class="text-sm font-semibold text-zinc-400 uppercase tracking-wide">Usage Over Time</h3>
    </div>
    <!-- Sparkline strip (added) -->
    <div id="history-sparkline-strip"></div>
    <div id="chart-empty" class="hidden text-zinc-500 italic text-sm py-8 text-center">No data for selected range.</div>
    <div id="chart-wrap"><canvas id="chart-usage" height="200"></canvas></div>
</div>
```

### Step 3 — Update `loadHistory` in `app.js`

Add history filter state at the top of `app.js` (near the other state variables):

```javascript
// History tab state
const historyState = {
    days: 7,
    activeProviders: null, // null = all; Set<string> when filtering
};
```

Add `toggleHistoryProvider` as a global:

```javascript
window.toggleHistoryProvider = function(pid) {
    if (!historyState.activeProviders) {
        // All active → select only this one
        historyState.activeProviders = new Set([pid]);
    } else if (historyState.activeProviders.has(pid)) {
        historyState.activeProviders.delete(pid);
        if (historyState.activeProviders.size === 0) historyState.activeProviders = null;
    } else {
        historyState.activeProviders.add(pid);
    }
    renderHistoryFromCache();
};
```

Add a module-level cache variable:

```javascript
let _historyCache = [];
```

Add `renderHistoryFromCache` (separates fetch from render):

```javascript
function renderHistoryFromCache() {
    const history = _historyCache;
    // Sparkline strip (always all providers, 7d)
    const stripEl = document.getElementById('history-sparkline-strip');
    if (stripEl) stripEl.innerHTML = buildProviderSparklineStrip(history, historyState.activeProviders);

    // Filter history for chart + table
    let filtered = history;
    if (historyState.activeProviders) {
        filtered = history.filter(s => historyState.activeProviders.has(s.provider_id));
    }
    updateCharts(filtered);

    // Table
    const container = document.getElementById('history-content');
    if (!filtered || filtered.length === 0) {
        container.innerHTML = '<p class="text-zinc-500 italic">No history data found.</p>';
        return;
    }
    let html = `<table class="w-full text-left mono text-[11px]">
        <thead class="text-zinc-600 border-b border-zinc-800/50">
            <tr>
                <th class="py-2 px-2">Time (UTC)</th>
                <th class="py-2 px-2">Provider</th>
                <th class="py-2 px-2">Service</th>
                <th class="py-2 px-2 text-right">Usage</th>
            </tr>
        </thead>
        <tbody class="text-zinc-400">`;
    filtered.slice(0, 50).forEach(s => {
        const date = new Date(s.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        const usage = s.used_value !== null ? `${s.used_value.toLocaleString()}${s.unit_type === 'percent' ? '%' : ''}` : '—';
        html += `<tr class="border-b border-zinc-900/30 hover:bg-zinc-800/10 transition-colors">
            <td class="py-2 px-2 text-zinc-600">${date}</td>
            <td class="py-2 px-2 text-zinc-500">${escapeHTML(s.provider_id || '—')}</td>
            <td class="py-2 px-2 font-medium text-zinc-300">${escapeHTML(s.service_name)}</td>
            <td class="py-2 px-2 text-right font-bold text-zinc-400">${usage}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}
```

Replace the existing `loadHistory` function:

```javascript
async function loadHistory() {
    const container = document.getElementById('history-content');
    container.innerHTML = '<p class="text-zinc-500 animate-pulse">Loading history...</p>';

    try {
        const history = await fetchHistory({ days: historyState.days, limit: 500 });
        _historyCache = history || [];
        renderHistoryFromCache();
    } catch (err) {
        destroyCharts();
        container.innerHTML = `<p class="text-red-400">Failed to load history: ${escapeHTML(err.message)}</p>`;
    }
}
```

Update the import in `app.js` to include `buildProviderSparklineStrip`:

```javascript
import { buildCard, buildModalContent, buildGitHubOAuthModal, buildProviderSection, buildProviderSummaryCard, buildProviderModal, buildProviderSparklineStrip, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildHealthBar } from './components.js';
```

### Step 4 — Verify in browser

Switch to the History tab. A row of small provider sparkline cards should appear above the chart. Each shows a mini SVG trend + current value + trend arrow. Clicking a card dims it and filters the chart to that provider. Clicking again deselects.

### Step 5 — Commit

```bash
git add frontend/js/components.js frontend/js/app.js frontend/index.html
git commit -m "feat(ui): add provider sparkline strip to history tab"
```

---

## Task 6: History Chart Controls (Time Range + Metric Switcher)

**Files:**
- Modify: `frontend/index.html` (add controls row to History chart panel)
- Modify: `frontend/js/app.js` (wire controls to `historyState`, update `loadHistory`)
- Modify: `frontend/js/charts.js` (add `metric` param support)
- Modify: `frontend/js/api.js` (update `fetchHistory` default limit)

### Step 1 — Add controls row to `index.html`

Inside `#history-chart-panel`, replace the existing header div with one that includes the controls:

```html
<div id="history-chart-panel" class="glass-panel rounded-3xl p-6 mb-6">
    <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
        <h3 class="text-sm font-semibold text-zinc-400 uppercase tracking-wide">Usage Over Time</h3>
        <div class="flex items-center gap-3 flex-wrap">
            <!-- Time range -->
            <div class="flex gap-1" id="history-range-btns">
                <button class="toggle-btn active" data-days="7"  onclick="setHistoryDays(7)">7d</button>
                <button class="toggle-btn"        data-days="30" onclick="setHistoryDays(30)">30d</button>
                <button class="toggle-btn"        data-days="90" onclick="setHistoryDays(90)">90d</button>
            </div>
            <!-- Metric -->
            <div class="flex gap-1" id="history-metric-btns">
                <button class="toggle-btn active" data-metric="percent" onclick="setHistoryMetric('percent')">% used</button>
                <button class="toggle-btn"        data-metric="tokens"  onclick="setHistoryMetric('tokens')">tokens</button>
                <button class="toggle-btn"        data-metric="cost"    onclick="setHistoryMetric('cost')">cost</button>
            </div>
        </div>
    </div>
    <!-- Sparkline strip -->
    <div id="history-sparkline-strip"></div>
    <div id="chart-empty" class="hidden text-zinc-500 italic text-sm py-8 text-center">No data for selected range.</div>
    <div id="chart-wrap"><canvas id="chart-usage" height="200"></canvas></div>
</div>
```

Also update the CSV download button to include active filters:

```html
<a id="csv-download-btn"
   href="/api/v1/usage/history?format=csv"
   download
   class="toggle-btn text-sm">
    Download CSV
</a>
```

(The href will be updated dynamically by `app.js`.)

### Step 2 — Add `setHistoryDays`, `setHistoryMetric`, and `updateCsvHref` to `app.js`

Add `metric` to `historyState`:

```javascript
const historyState = {
    days: 7,
    activeProviders: null,
    metric: 'percent', // 'percent' | 'tokens' | 'cost'
};
```

Add globals after `toggleHistoryProvider`:

```javascript
window.setHistoryDays = function(days) {
    historyState.days = days;
    // Update button active state
    document.querySelectorAll('#history-range-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', parseInt(btn.dataset.days) === days);
    });
    updateCsvHref();
    loadHistory();
};

window.setHistoryMetric = function(metric) {
    historyState.metric = metric;
    document.querySelectorAll('#history-metric-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.metric === metric);
    });
    // Re-render chart with new metric (no refetch needed)
    updateCharts(_historyCache, historyState.metric);
};

function updateCsvHref() {
    const btn = document.getElementById('csv-download-btn');
    if (!btn) return;
    const params = new URLSearchParams({ format: 'csv', days: historyState.days });
    if (historyState.activeProviders && historyState.activeProviders.size > 0) {
        // CSV endpoint supports provider_id (single value); if multiple, omit filter
        if (historyState.activeProviders.size === 1) {
            params.set('provider_id', [...historyState.activeProviders][0]);
        }
    }
    btn.href = `/api/v1/usage/history?${params.toString()}`;
}
```

Also update `renderHistoryFromCache` to pass `historyState.metric` to `updateCharts`:

```javascript
// in renderHistoryFromCache, replace:
updateCharts(filtered);
// with:
updateCharts(filtered, historyState.metric);
```

And call `updateCsvHref()` from `loadHistory()`:

```javascript
async function loadHistory() {
    const container = document.getElementById('history-content');
    container.innerHTML = '<p class="text-zinc-500 animate-pulse">Loading history...</p>';
    updateCsvHref(); // keep href in sync with current filters
    try {
        const history = await fetchHistory({ days: historyState.days, limit: 500 });
        _historyCache = history || [];
        renderHistoryFromCache();
    } catch (err) {
        destroyCharts();
        container.innerHTML = `<p class="text-red-400">Failed to load history: ${escapeHTML(err.message)}</p>`;
    }
}
```

Also update `toggleHistoryProvider` to call `updateCsvHref()`:

```javascript
window.toggleHistoryProvider = function(pid) {
    // ... existing toggle logic ...
    updateCsvHref();
    renderHistoryFromCache();
};
```

### Step 3 — Update `charts.js` to support metric parameter

Replace `updateCharts` in `frontend/js/charts.js`:

```javascript
/**
 * @param {Array} snapshots - history snapshot objects
 * @param {'percent'|'tokens'|'cost'} [metric='percent'] - which value to plot
 */
export function updateCharts(snapshots, metric = 'percent') {
    destroyCharts();

    const canvas = document.getElementById("chart-usage");
    const emptyEl = document.getElementById("chart-empty");
    if (!canvas) return;

    if (!snapshots || snapshots.length === 0) {
        emptyEl?.classList.remove("hidden");
        document.getElementById("chart-wrap")?.classList.add("hidden");
        return;
    }
    emptyEl?.classList.add("hidden");
    document.getElementById("chart-wrap")?.classList.remove("hidden");

    const { labels, providers } = extractLabelsAndProviders(snapshots);
    const buckets = bucketByDayMetric(snapshots, metric);

    const yLabel = metric === 'cost' ? 'Cost (USD)' : metric === 'tokens' ? 'Tokens' : '% Used';

    const datasets = providers.map(provider => {
        const color = colorFor(provider);
        return {
            label: provider.toUpperCase(),
            data: labels.map(day => {
                const b = buckets[day]?.[provider];
                return b ? parseFloat((b.sum / b.count).toFixed(2)) : null;
            }),
            borderColor: color,
            backgroundColor: color + "15",
            borderWidth: 2,
            tension: 0.3,
            spanGaps: true,
            pointRadius: 2,
            pointHoverRadius: 5,
            fill: true,
        };
    });

    const options = {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 300 },
        plugins: {
            legend: {
                position: 'top', align: 'end',
                labels: { color: "#71717a", font: { size: 10, weight: 'bold' }, usePointStyle: true, boxWidth: 6 }
            },
            tooltip: {
                mode: "index", intersect: false,
                backgroundColor: 'rgba(24, 24, 27, 0.95)',
                titleColor: '#f4f4f5', bodyColor: '#a1a1aa',
                borderColor: 'rgba(63, 63, 70, 0.5)', borderWidth: 1,
                padding: 10, bodyFont: { family: 'JetBrains Mono' }
            },
        },
        scales: {
            x: { ticks: { color: "#52525b", font: { size: 9 }, maxTicksLimit: 7 }, grid: { display: false } },
            y: {
                beginAtZero: true,
                ticks: { color: "#52525b", font: { size: 9 }, maxTicksLimit: 5 },
                grid: { color: "rgba(39, 39, 42, 0.5)" },
                title: { display: true, text: yLabel, color: '#52525b', font: { size: 9 } },
            },
        },
    };

    _chart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels, datasets },
        options,
    });
}
```

Replace `bucketByDay` with `bucketByDayMetric`:

```javascript
/**
 * Bucket snapshots by day and provider, extracting the correct metric.
 * @param {Array} snapshots
 * @param {'percent'|'tokens'|'cost'} metric
 * @returns {Object} { "YYYY-MM-DD": { provider_id: { sum, count } } }
 */
function bucketByDayMetric(snapshots, metric) {
    const buckets = {};
    for (const snap of snapshots) {
        let value;
        if (metric === 'cost') {
            if (snap.unit_type !== 'currency' || snap.used_value == null) continue;
            value = snap.used_value;
        } else if (metric === 'tokens') {
            if (snap.unit_type !== 'tokens' || snap.used_value == null) continue;
            value = snap.used_value;
        } else {
            // percent: derive from used/limit or use direct percent value
            if (snap.unit_type === 'percent' && snap.used_value != null) {
                value = snap.used_value;
            } else if (snap.used_value != null && snap.limit_value > 0) {
                value = (snap.used_value / snap.limit_value) * 100;
            } else {
                continue;
            }
        }
        const day = snap.timestamp.slice(0, 10);
        const pid = snap.provider_id || "unknown";
        if (!buckets[day]) buckets[day] = {};
        if (!buckets[day][pid]) buckets[day][pid] = { sum: 0, count: 0 };
        buckets[day][pid].sum += value;
        buckets[day][pid].count += 1;
    }
    return buckets;
}
```

Remove the old `bucketByDay` function. Keep `extractLabelsAndProviders`, `colorFor`, `destroyCharts`, `setChartView` unchanged.

### Step 4 — Verify in browser

On the History tab:
- Three time range buttons (7d / 30d / 90d). Clicking each reloads history and updates the chart.
- Three metric buttons (% used / tokens / cost). Clicking switches the Y-axis. Empty chart if no data for that metric type.
- CSV download URL updates to include `?days=30&format=csv` etc.
- Sparkline strip cards remain, provider filter still works.

### Step 5 — Commit

```bash
git add frontend/js/app.js frontend/js/charts.js frontend/index.html
git commit -m "feat(ui): add time range, metric switcher, and CSV filter to history tab"
```

---

## Self-Review

**Spec coverage check:**

| Spec item | Task |
|---|---|
| 1A Health bar — 4 tiles + proportional bar | Task 1 ✓ |
| 1B Provider cards — two-zone, tier badge, account label, worst metric, segmented bar, breakdown rows | Task 2+3 ✓ |
| 1B Provider cards sorted worst-first | Task 3 ✓ |
| 1C Modal — sparklines per service, used/limit, reset, source, pace | Task 4 ✓ |
| 2A History sparkline strip — per-provider, trend arrow, click to filter | Task 5 ✓ |
| 2B Time range (7d/30d/90d) | Task 6 ✓ |
| 2B Per-provider toggle chips | Task 5 (sparkline cards act as toggles) ✓ |
| 2B Metric switcher (% / tokens / cost) | Task 6 ✓ |
| 2C CSV download inherits filters | Task 6 ✓ |

**Not in this plan (separate Plan 2):**
- Settings sidebar layout
- Providers master-detail section with API key config
- Tokens / Webhooks / System sections
- `provider_configs` backend table + API

**Type/name consistency check:**
- `buildProviderSparklineStrip(history, activeProviders)` — called with `(history, historyState.activeProviders)` ✓
- `updateCharts(snapshots, metric)` — called with `(_historyCache, historyState.metric)` ✓
- `buildSparklineSVG` is internal (not exported) but called from `buildProviderModal` and `buildProviderSparklineStrip`, both in same file ✓
- `getTrendArrow`, `getTierTextClass` are internal helpers defined before use ✓
- `historyState` and `_historyCache` are module-level in `app.js`, referenced by all history functions ✓
- `HEALTH_SEVERITY` is defined locally in both `components.js` functions and `app.js` `renderGrid` — intentional duplication, no shared import needed ✓
