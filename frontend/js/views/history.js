import { fetchHistory, fetchHistoryRaw } from '../api.js';
import { buildProviderSparklineStrip } from '../components.js';
import { updateCharts, destroyCharts } from '../charts.js';

const historyState = {
    days: 1,
    activeProviders: null, // Set of provider IDs
    metric: 'percent',
    windowFilter: 'all',
    showPeaks: false,
    page: 1,
};
let _historyCache = [];
let _historyRawCache = [];

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

export function setHistoryPeak(enabled) {
    historyState.showPeaks = enabled;
    document.querySelectorAll('#history-peak-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.peak === String(enabled));
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

function formatValue(value, unit) {
    if (value === null || value === undefined) return '—';
    // Treat 0 and very small values as empty
    if (value === 0 || value < 0.01) return '—';
    const unitStr = unit || '';
    if (unitStr === 'percent') return `${value.toFixed(1)}%`;
    if (unitStr === 'currency') return `$${value.toFixed(2)}`;
    if (unitStr === 'tokens') return value.toLocaleString();
    if (unitStr === 'requests') return `${value.toLocaleString()} requests`;
    return `${value.toLocaleString()}${unitStr}`;
}

function formatWindowValue(entry) {
    if (!entry) return '—';
    return formatValue(entry.value, entry.unit);
}

function parseAdditional(additionalStr) {
    if (!additionalStr) return [];
    const items = [];
    additionalStr.split(' | ').forEach(part => {
        const match = part.match(/^(.+?): (\d+(?:\.\d+)?)(.*)$/);
        if (match) {
            items.push({
                window_type: match[1],
                value: parseFloat(match[2]),
                unit: match[3] || 'generic'
            });
        }
    });
    return items;
}

export function renderHistoryFromCache(skipChartUpdate = false) {
    const history = _historyCache;
    const rawHistory = _historyRawCache || [];
    const stripEl = document.getElementById('history-sparkline-strip');

    // Build sparklines from RAW data for chart (each provider+window as separate line)
    const sparklineData = [];
    rawHistory.forEach(row => {
        if (row.used_value == null) return;
        // Filter by metric selector
        const metric = historyState.metric;
        if (metric === 'percent' && row.unit_type !== 'percent') return;
        if (metric === 'tokens' && row.unit_type !== 'tokens') return;
        if (metric === 'cost' && row.unit_type !== 'currency') return;
        sparklineData.push({
            provider_id: row.provider_id,
            timestamp: row.timestamp,
            used_value: row.used_value,
            limit_value: row.limit_value,
            unit_type: row.unit_type,
            window_type: row.window_type
        });
    });
    if (stripEl) stripEl.innerHTML = buildProviderSparklineStrip(sparklineData, historyState.activeProviders, historyState.days);

    // Update charts with the same adapted data
    if (!skipChartUpdate) {
        updateCharts(sparklineData, historyState.metric, historyState.days, historyState.windowFilter, historyState.showPeaks);
    }

    const container = document.getElementById('history-content');
    if (!history || history.length === 0) {
        container.innerHTML = '<p class="text-zinc-500 italic">No history data found.</p>';
        return;
    }

    // Filter by provider if active
    let filtered = history;
    if (historyState.activeProviders) {
        filtered = history.filter(s => historyState.activeProviders.has(s.provider_id));
    }

    // Apply window filter to table (affects what we show in session/weekly columns)
    let tableData = filtered;
    if (historyState.windowFilter !== 'all') {
        tableData = filtered.filter(s => {
            if (historyState.windowFilter === 'session') return !!s.session;
            if (historyState.windowFilter === 'weekly') return !!s.weekly;
            return true;
        });
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
                <th class="py-2 px-2">Account</th>
                <th class="py-2 px-2 text-right">Session</th>
                <th class="py-2 px-2 text-right">Weekly</th>
                <th class="py-2 px-2">Additional</th>
            </tr>
        </thead>
        <tbody class="text-zinc-400">`;
    pageData.forEach(s => {
        const date = new Date(s.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        const session = formatWindowValue(s.session);
        const weekly = formatWindowValue(s.weekly);
        const additional = s.additional || '—';

        html += `<tr class="border-b border-zinc-900/30 hover:bg-zinc-800/10 transition-colors">
            <td class="py-2 px-2 text-zinc-600">${date}</td>
            <td class="py-2 px-2 text-zinc-500 font-medium">${escapeHTML(s.provider_id || '—')}</td>
            <td class="py-2 px-2 text-zinc-500 italic">${escapeHTML(s.account_label || '—')}</td>
            <td class="py-2 px-2 text-right font-bold text-zinc-300">${session}</td>
            <td class="py-2 px-2 text-right font-bold text-zinc-300">${weekly}</td>
            <td class="py-2 px-2 text-zinc-500 italic">${escapeHTML(additional)}</td>
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
        // Fetch grouped data for table
        const response = await fetchHistoryCached({ days: historyState.days, limit: 1000 });
        _historyCache = response?.averages || [];
        window._historyPeaks = response?.peaks || [];

        // Fetch raw data for chart (each provider+window as separate line)
        try {
            const rawResponse = await fetchHistoryRaw({ days: historyState.days, limit: 1000 });
            _historyRawCache = rawResponse || [];
        } catch (rawErr) {
            console.warn('Failed to fetch raw history for chart:', rawErr);
            _historyRawCache = [];
        }

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
    window.setHistoryPeak = setHistoryPeak;
    window.toggleHistoryProvider = toggleHistoryProvider;
    window.setHistoryPage = setHistoryPage;
}