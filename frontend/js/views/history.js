import { fetchHistory } from '../api.js';
import { buildProviderSparklineStrip } from '../components.js';
import { updateCharts, destroyCharts } from '../charts.js';

const historyState = {
    days: 1,
    activeProviders: null, // Set of provider IDs
    metric: 'percent',
    windowFilter: 'all',
    page: 1,
};
let _historyCache = [];

// History cache for stale-while-revalidate pattern
const CACHE_TTL_MS = 30_000;
const _historyCacheStore = new Map();

function getCacheKey(params) {
    return `${params.provider_id || 'all'}:${params.days}:${params.limit || 500}`;
}

export async function fetchHistoryCached(params) {
    const key = getCacheKey(params);
    const now = Date.now();
    const cached = _historyCacheStore.get(key);
    
    if (cached && (now - cached.timestamp) < CACHE_TTL_MS) {
        return cached.data;
    }
    
    const data = await fetchHistory(params);
    _historyCacheStore.set(key, { data, timestamp: now });
    return data;
}

export function clearHistoryCache() {
    _historyCacheStore.clear();
}

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

export function getHistoryState() {
    return historyState;
}

export function setHistoryDays(days) {
    historyState.days = days;
    historyState.page = 1;
    updateCsvHref();
    loadHistoryView();
}

export function setHistoryMetric(metric) {
    historyState.metric = metric;
    historyState.page = 1;
    document.querySelectorAll('#history-metric-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.metric === metric);
    });
    renderHistoryFromCache();
}

export function setHistoryWindow(windowType) {
    historyState.windowFilter = windowType;
    historyState.page = 1;
    document.querySelectorAll('#history-window-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.window === windowType);
    });
    renderHistoryFromCache();
}

export function toggleHistoryProvider(pid) {
    historyState.page = 1;
    if (!historyState.activeProviders) {
        historyState.activeProviders = new Set([pid]);
    } else if (historyState.activeProviders.has(pid)) {
        historyState.activeProviders.delete(pid);
        if (historyState.activeProviders.size === 0) historyState.activeProviders = null;
    } else {
        historyState.activeProviders.add(pid);
    }
    updateCsvHref();
    renderHistoryFromCache();
}

function updateCsvHref() {
    const btn = document.getElementById('csv-download-btn');
    if (!btn) return;
    const params = new URLSearchParams({ format: 'csv', days: historyState.days });
    if (historyState.activeProviders && historyState.activeProviders.size === 1) {
        params.set('provider_id', [...historyState.activeProviders][0]);
    }
    btn.href = `/api/v1/usage/history?${params.toString()}`;
}

export function renderHistoryFromCache(skipChartUpdate = false) {
    const history = _historyCache;
    const stripEl = document.getElementById('history-sparkline-strip');
    if (stripEl) stripEl.innerHTML = buildProviderSparklineStrip(history, historyState.activeProviders, historyState.days);

    let filtered = history;
    if (historyState.activeProviders) {
        filtered = history.filter(s => historyState.activeProviders.has(s.provider_id));
    }
    
    // updateCharts now handles windowFilter internally or we can filter here
    if (!skipChartUpdate) {
        updateCharts(filtered, historyState.metric, historyState.days, historyState.windowFilter);
    }

    const container = document.getElementById('history-content');
    if (!filtered || filtered.length === 0) {
        container.innerHTML = '<p class="text-zinc-500 italic">No history data found.</p>';
        return;
    }
    
    // Apply window filter to the table
    let tableData = filtered;
    if (historyState.windowFilter !== 'all') {
        tableData = filtered.filter(s => (s.window_type || 'unknown').toLowerCase() === historyState.windowFilter);
    }
    
    const totalItems = tableData.length;
    const pageSize = 20;
    const totalPages = Math.ceil(totalItems / pageSize);
    const start = (historyState.page - 1) * pageSize;
    const pageData = tableData.slice(start, start + pageSize);

    let html = `<table class="w-full text-left mono text-[11px]">
        <thead class="text-zinc-600 border-b border-zinc-800/50">
            <tr>
                <th class="py-2 px-2">Time</th>
                <th class="py-2 px-2">Provider</th>
                <th class="py-2 px-2">Service (Window)</th>
                <th class="py-2 px-2">Source</th>
                <th class="py-2 px-2">Method</th>
                <th class="py-2 px-2 text-right">Usage</th>
            </tr>
        </thead>
        <tbody class="text-zinc-400">`;
    pageData.forEach(s => {
        const date = new Date(s.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        const usage = s.used_value !== null ? `${s.used_value.toLocaleString()}${s.unit_type === 'percent' ? '%' : ''}` : '—';
        const source = s.sidecar_id || 'local';
        const method = (s.data_source || 'unknown').replace('_', ' ');
        
        html += `<tr class="border-b border-zinc-900/30 hover:bg-zinc-800/10 transition-colors">
            <td class="py-2 px-2 text-zinc-600">${date}</td>
            <td class="py-2 px-2 text-zinc-500">${escapeHTML(s.provider_id || '—')}</td>
            <td class="py-2 px-2 font-medium text-zinc-300">${escapeHTML(s.service_name || '—')} <span class="text-[9px] text-zinc-600 uppercase">(${escapeHTML(s.window_type || '—')})</span></td>
            <td class="py-2 px-2 text-zinc-500 italic">${escapeHTML(source)}</td>
            <td class="py-2 px-2"><span class="px-1.5 py-0.5 rounded-md bg-zinc-800/50 text-[9px] uppercase text-zinc-500">${escapeHTML(method)}</span></td>
            <td class="py-2 px-2 text-right font-bold text-zinc-400">${usage}</td>
        </tr>`;
    });
    html += '</tbody></table>';

    if (totalPages > 1) {
        html += `<div class="mt-6 flex items-center justify-between">
            <div class="text-[10px] text-zinc-600 uppercase tracking-widest">
                Showing ${start + 1}–${Math.min(start + pageSize, totalItems)} of ${totalItems}
            </div>
            <div class="flex items-center gap-1">
                <button class="toggle-btn px-4 py-1" ${historyState.page <= 1 ? 'disabled' : ''} onclick="setHistoryPage(${historyState.page - 1})">
                    Previous
                </button>
                <div class="px-3 text-[11px] font-bold text-zinc-400 mono">
                    ${historyState.page} <span class="text-zinc-700 mx-1">/</span> ${totalPages}
                </div>
                <button class="toggle-btn px-4 py-1" ${historyState.page >= totalPages ? 'disabled' : ''} onclick="setHistoryPage(${historyState.page + 1})">
                    Next
                </button>
            </div>
        </div>`;
    }

    container.innerHTML = html;
}

export function setHistoryPage(page) {
    historyState.page = page;
    renderHistoryFromCache(true);
}

export async function loadHistoryView() {
    updateCsvHref();
    const container = document.getElementById('history-content');
    if (container) container.innerHTML = '<p class="text-zinc-500 animate-pulse">Loading history...</p>';

    try {
        const history = await fetchHistoryCached({ days: historyState.days, limit: 1000 });
        _historyCache = history || [];
        renderHistoryFromCache();
    } catch (err) {
        destroyCharts();
        if (container) container.innerHTML = `<p class="text-red-400">Failed to load history: ${escapeHTML(err.message)}</p>`;
    }
}

export function initHistoryView() {
    // These functions should be globally available for the onclick handlers in index.html
    window.setHistoryDays = setHistoryDays;
    window.setHistoryMetric = setHistoryMetric;
    window.setHistoryWindow = setHistoryWindow;
    window.toggleHistoryProvider = toggleHistoryProvider;
    window.setHistoryPage = setHistoryPage;
}