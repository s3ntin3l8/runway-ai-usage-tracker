import { fetchHistory } from '../api.js';
import { buildProviderSparklineStrip } from '../components.js';
import { updateCharts, destroyCharts } from '../charts.js';

const historyState = {
    days: 7,
    activeProviders: null,
    metric: 'percent',
};
let _historyCache = [];

// History cache for stale-while-revalidate pattern
const CACHE_TTL_MS = 30_000; // 30 seconds
const _historyCacheStore = new Map();

function getCacheKey(params) {
    return `${params.provider_id || 'all'}:${params.days}:${params.limit || 500}`;
}

export async function fetchHistoryCached(params) {
    const key = getCacheKey(params);
    const now = Date.now();
    const cached = _historyCacheStore.get(key);
    
    // Return stale cache immediately if available
    if (cached && (now - cached.timestamp) < CACHE_TTL_MS) {
        return cached.data;
    }
    
    // Fetch fresh data
    const data = await fetchHistory(params);
    
    // Update cache
    _historyCacheStore.set(key, { data, timestamp: now });
    
    return data;
}

export function clearHistoryCache() {
    _historyCacheStore.clear();
}

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&gt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

export function getHistoryState() {
    return historyState;
}

export function setHistoryDays(days) {
    historyState.days = days;
    document.querySelectorAll('#history-range-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', parseFloat(btn.dataset.days) === days);
    });
    updateCsvHref();
    loadHistoryView();
}

export function setHistoryMetric(metric) {
    historyState.metric = metric;
    document.querySelectorAll('#history-metric-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.metric === metric);
    });
    renderHistoryFromCache();
}

export function toggleHistoryProvider(pid) {
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

function renderHistoryFromCache() {
    const history = _historyCache;
    const stripEl = document.getElementById('history-sparkline-strip');
    if (stripEl) stripEl.innerHTML = buildProviderSparklineStrip(history, historyState.activeProviders, historyState.days);

    let filtered = history;
    if (historyState.activeProviders) {
        filtered = history.filter(s => historyState.activeProviders.has(s.provider_id));
    }
    updateCharts(filtered, historyState.metric, historyState.days);

    const container = document.getElementById('history-content');
    if (!filtered || filtered.length === 0) {
        container.innerHTML = '<p class="text-zinc-500 italic">No history data found.</p>';
        return;
    }
    let html = `<table class="w-full text-left mono text-[11px]">
        <thead class="text-zinc-600 border-b border-zinc-800/50">
            <tr>
                <th class="py-2 px-2">Time</th>
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
            <td class="py-2 px-2 font-medium text-zinc-300">${escapeHTML(s.service_name || '—')}</td>
            <td class="py-2 px-2 text-right font-bold text-zinc-400">${usage}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

export async function loadHistoryView() {
    updateCsvHref();
    const container = document.getElementById('history-content');
    container.innerHTML = '<p class="text-zinc-500 animate-pulse">Loading history...</p>';

    try {
        const history = await fetchHistoryCached({ days: historyState.days, limit: 500 });
        _historyCache = history || [];
        renderHistoryFromCache();
    } catch (err) {
        destroyCharts();
        container.innerHTML = `<p class="text-red-400">Failed to load history: ${escapeHTML(err.message)}</p>`;
    }
}

export function initHistoryView() {
    document.querySelectorAll('#history-range-btns .toggle-btn').forEach(btn => {
        btn.addEventListener('click', () => setHistoryDays(parseFloat(btn.dataset.days)));
    });
    document.querySelectorAll('#history-metric-btns .toggle-btn').forEach(btn => {
        btn.addEventListener('click', () => setHistoryMetric(btn.dataset.metric));
    });
}