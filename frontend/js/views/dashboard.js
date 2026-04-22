// Dashboard view module - lazy loaded via dynamic import
import { fetchLimits } from '../api.js';
import { STATE } from '../state.js';
import { buildProviderSummaryCard, buildHealthBar, buildProviderModal, buildModalSkeleton } from '../components.js';
import { fetchHistoryCached } from './history.js';
import { applyOrder, cardKey } from '../layout.js';

let loadDataGeneration = 0;

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

export function applyFilters(data) {
    const f = STATE.activeFilter;
    return data.filter(item => {
        if (f && item[f.dimension] !== f.value) return false;
        return true;
    });
}

export function renderGrid() {
    const grid = document.getElementById('grid');
    if (!grid) return;

    const visible = applyFilters(STATE.data);

    const groups = new Map();
    visible.forEach(item => {
        const key = item.provider_id || '__other__';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(item);
    });

    const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };
    const defaultSorted = [...groups.keys()].sort((a, b) => {
        if (a === '__other__') return 1;
        if (b === '__other__') return -1;
        const aWorst = groups.get(a).reduce((m, i) => Math.max(m, HEALTH_SEVERITY[i.health] || 0), 0);
        const bWorst = groups.get(b).reduce((m, i) => Math.max(m, HEALTH_SEVERITY[i.health] || 0), 0);
        if (bWorst !== aWorst) return bWorst - aWorst;
        return a.localeCompare(b);
    });
    const sorted = applyOrder(
        defaultSorted.map(pid => ({ pid })),
        x => x.pid,
        STATE.layout?.provider_order ?? []
    ).map(x => x.pid);

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

    grid.innerHTML = `<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">${html}</div>`;
    const footerCount = document.getElementById('footer-count');
    if (footerCount) footerCount.textContent = count;
}

export function renderHealthBar() {
    const el = document.getElementById('health-bar');
    if (!el) return;
    el.innerHTML = buildHealthBar(STATE.data);
}

export function renderFilterPills() {
    const container = document.getElementById('filter-pills');
    if (!container) return;

    const dim = STATE.filterDimension;
    const WINDOW_ORDER = ['session', 'daily', 'weekly', 'biweekly', 'monthly', 'prepaid', 'unknown'];
    const rawValues = [...new Set(STATE.data.map(i => i[dim]).filter(Boolean))];
    const values = dim === 'window_type'
        ? rawValues.sort((a, b) => {
            const ai = WINDOW_ORDER.indexOf(a), bi = WINDOW_ORDER.indexOf(b);
            if (ai === -1 && bi === -1) return a.localeCompare(b);
            if (ai === -1) return 1;
            if (bi === -1) return -1;
            return ai - bi;
          })
        : rawValues.sort();
    const active = STATE.activeFilter?.value;

    const pills = [`<button class="pill${!active ? ' pill-active' : ''}" data-filter-value="">All</button>`];
    values.forEach(v => {
        pills.push(`<button class="pill${active === v ? ' pill-active' : ''}" data-filter-value="${escapeHTML(v)}">${escapeHTML(v)}</button>`);
    });
    container.innerHTML = pills.join('');

    // Event delegation: one listener handles all pill clicks via data-filter-value
    if (!container._pillListenerAttached) {
        container._pillListenerAttached = true;
        container.addEventListener('click', e => {
            const btn = e.target.closest('button[data-filter-value]');
            if (!btn) return;
            const val = btn.dataset.filterValue;
            setFilter(val || null);
        });
    }

    const hasSidecars = STATE.data.some(i => i.sidecar_id);
    const sidecarBtn = document.getElementById('dim-btn-sidecar');
    if (sidecarBtn) sidecarBtn.classList.toggle('hidden', !hasSidecars);

    document.querySelectorAll('.dim-btn').forEach(btn => {
        btn.classList.toggle('dim-btn-active', btn.dataset.dim === dim);
    });
}

export async function loadDashboard() {
    const myGeneration = ++loadDataGeneration;

    const grid = document.getElementById('grid');
    const loading = document.getElementById('loading');
    const errorBanner = document.getElementById('error-banner');
    const lastUpdated = document.getElementById('last-updated');

    if (grid) {
        grid.innerHTML = '';
        grid.classList.add('hidden');
    }
    if (loading) loading.classList.remove('hidden');
    if (errorBanner) errorBanner.classList.add('hidden');

    try {
        const json = await fetchLimits();
        if (myGeneration !== loadDataGeneration) return;
        STATE.data = json.limits;
        renderFilterPills();
        renderGrid();
        renderHealthBar();

        const now = new Date();
        if (lastUpdated) {
            lastUpdated.textContent = `Updated ${now.toLocaleTimeString()}`;
            lastUpdated.classList.remove('hidden');
        }
        window._lastFetchTime = Date.now();
    } catch (err) {
        if (myGeneration !== loadDataGeneration) return;
        console.error('Failed to fetch limits:', err);
        const errorMsg = err.message || 'Unknown error occurred';
        const displayMsg = `⚠ ${errorMsg}`;
        if (errorBanner) {
            errorBanner.textContent = displayMsg;
            errorBanner.classList.remove('hidden');
        }
    } finally {
        if (loading) loading.classList.add('hidden');
        if (grid) grid.classList.remove('hidden');
    }
}

export function setFilter(value) {
    STATE.activeFilter = value ? { dimension: STATE.filterDimension, value } : null;
    localStorage.setItem('runway_active_filter', JSON.stringify(STATE.activeFilter));
    renderFilterPills();
    renderGrid();
}

export function setFilterDimension(dim) {
    STATE.filterDimension = dim;
    STATE.activeFilter = null;
    localStorage.setItem('runway_filter_dimension', dim);
    localStorage.removeItem('runway_active_filter');
    renderFilterPills();
    renderGrid();
}

export function initDashboardView() {
    // Filter buttons
    document.querySelectorAll('.dim-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            setFilterDimension(btn.dataset.dim);
        });
    });

    // Clear filter
    document.getElementById('filter-pills')?.addEventListener('click', (e) => {
        if (e.target.classList.contains('hover:text-white')) {
            setFilter(null);
        }
    });

    // Card click delegation
    document.getElementById('grid')?.addEventListener('click', (e) => {
        const card = e.target.closest('.card-clickable');
        if (card) {
            const providerId = card.dataset.providerId;
            openProviderModal(providerId);
        }
    });
}

export async function openProviderModal(providerId) {
    let items = STATE.data.filter(d => (d.provider_id || '__other__') === providerId);
    items = applyOrder(items, cardKey, STATE.layout?.card_orders?.[providerId] ?? []);
    if (!items.length) return;

    const container = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');
    if (!container || !content) return;

    // Keep order already applied by applyOrder (pinned first, then unpinned).
    // For unpinned items, preserve API order; user can reorder via edit mode.
    const sorted = items;

    // Show skeleton immediately while fetching
    content.innerHTML = buildModalSkeleton(items.length);
    container.classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('modal-backdrop').onclick = () => {
        container.classList.remove('active');
        document.body.style.overflow = '';
    };
    document.getElementById('close-modal').onclick = () => {
        container.classList.remove('active');
        document.body.style.overflow = '';
    };

    try {
        // Fetch history using cached fetch (may return stale data immediately)
        const history = await fetchHistoryCached({ provider_id: providerId, days: 7, limit: 500 });
        
        // Only update if modal is still open
        if (container.classList.contains('active') && content.querySelector('#close-modal')) {
            content.innerHTML = buildProviderModal(providerId, sorted, history);
            document.getElementById('close-modal').onclick = () => {
                container.classList.remove('active');
                document.body.style.overflow = '';
            };
            if (typeof window.__reattachCardSortables === 'function') await window.__reattachCardSortables();
        }
    } catch (e) {
        console.warn('Could not fetch history for modal sparklines:', e.message);
        // Still show the modal without sparklines
        if (container.classList.contains('active')) {
            content.innerHTML = buildProviderModal(providerId, sorted, []);
            if (typeof window.__reattachCardSortables === 'function') await window.__reattachCardSortables();
        }
    }
}