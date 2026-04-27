import { fetchHistory, fetchHistoryRaw } from '../api.js';
import { buildProviderSparklineStrip } from '../components.js';
import { updateCharts, destroyCharts } from '../charts.js';
import { STATE } from '../state.js';

const historyState = {
    days: 1,
    activeProviders: null, // Set of provider IDs (null = all)
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

export function setHistoryRange(days) {
    historyState.days = days;
    historyState.page = 1;
    document.querySelectorAll('#history-range-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', parseFloat(btn.dataset.range) === days);
    });
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

export function setHistoryProvidersAll() {
    historyState.activeProviders = null;
    historyState.page = 1;
    updateCsvHref();
    renderHistoryFromCache();
}

export function setHistoryProvidersNone() {
    historyState.activeProviders = new Set(); // empty set = nothing visible
    historyState.page = 1;
    updateCsvHref();
    renderHistoryFromCache();
}

function buildHistorySummary(rawHistory, filteredProviders, metric, days) {
    if (!rawHistory || rawHistory.length === 0) return '';
    let rows = rawHistory;
    if (filteredProviders) rows = rows.filter(r => filteredProviders.has(r.provider_id));
    if (rows.length === 0) return '';

    // Compute from percent-compatible rows only
    const pctRows = rows.filter(r => {
        if (r.used_value == null) return false;
        if (metric !== 'percent') return r.unit_type === metric;
        return r.unit_type === 'percent' || (r.limit_value > 0);
    }).map(r => {
        if (metric === 'percent') {
            return r.unit_type === 'percent' ? r.used_value : (r.used_value / r.limit_value) * 100;
        }
        return r.used_value;
    });

    if (pctRows.length === 0) return '';

    const avg = pctRows.reduce((s, v) => s + v, 0) / pctRows.length;
    const peak = Math.max(...pctRows);
    const critCount = rawHistory.filter(r => {
        if (r.used_value == null) return false;
        if (r.unit_type === 'percent') return r.used_value >= 90;
        if (r.limit_value > 0) return (r.used_value / r.limit_value) >= 0.9;
        return false;
    }).length;
    const providerCount = new Set(rows.map(r => r.provider_id)).size;
    const daysLabel = days >= 30 ? '30D' : days >= 7 ? '7D' : days >= 1 ? '24H' : '6H';
    const unit = metric === 'percent' ? '%' : metric === 'cost' ? ' USD' : '';

    let parts = [`Showing ${daysLabel} · ${providerCount} provider${providerCount !== 1 ? 's' : ''} · avg ${avg.toFixed(1)}${unit} · peak ${peak.toFixed(1)}${unit}`];
    if (critCount > 0) parts.push(`${critCount} crit events`);

    return parts.join(' · ');
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

// Convert an entry to match the active metric, or return null if incompatible.
// Used both to display GitHub (requests) as % when metric=percent, and to filter
// out rows that have no data in the active metric.
function adaptEntryToMetric(entry, metric) {
    if (!entry || entry.value == null) return null;
    if (metric === 'percent') {
        if (entry.unit === 'percent') return entry;
        if (entry.limit && entry.limit > 0) {
            return { ...entry, value: (entry.value / entry.limit) * 100, unit: 'percent' };
        }
        return null;
    }
    if (metric === 'tokens') {
        return entry.unit === 'tokens' ? entry : null;
    }
    if (metric === 'cost') {
        return entry.unit === 'currency' ? entry : null;
    }
    return entry;
}

const MODEL_LABEL_OVERRIDES = {
    sonnet: 'sonnet',
    opus: 'opus',
    design: 'design',
};

function friendlyWindowLabel(entry) {
    if (entry?.model_id && MODEL_LABEL_OVERRIDES[entry.model_id]) {
        return MODEL_LABEL_OVERRIDES[entry.model_id];
    }
    const w = entry?.window;
    if (!w) return '—';
    return w.replace(/^seven_day_/, '').replace(/_/g, ' ');
}

function renderAdditional(list) {
    if (!list || list.length === 0) return '—';
    return list.map(a => {
        const label = escapeHTML(friendlyWindowLabel(a));
        const val = formatValue(a.value, a.unit);
        return `<span class="ht-extra">${label} ${val}</span>`;
    }).join('');
}

export function renderHistoryFromCache(skipChartUpdate = false) {
    const history = _historyCache;
    const rawHistory = _historyRawCache || [];
    const stripEl = document.getElementById('history-sparkline-strip');

    // Update aggregate summary
    const summaryEl = document.getElementById('history-summary');
    if (summaryEl) {
        summaryEl.innerHTML = buildHistorySummary(rawHistory, historyState.activeProviders, historyState.metric, historyState.days);
    }

    // Render cross-view filter pill
    renderHistoryFilterPill();

    // Build sparklines from RAW data for chart (each provider+service+window as separate line)
    const sparklineData = [];
    rawHistory.forEach(row => {
        if (row.used_value == null) return;
        const metric = historyState.metric;
        let entry = row;
        if (metric === 'percent') {
            if (row.unit_type === 'percent') {
                // pass through
            } else if (row.limit_value && row.limit_value > 0) {
                // derive percent on the fly (mirrors dashboard sparkline behavior)
                entry = { ...row, used_value: (row.used_value / row.limit_value) * 100, unit_type: 'percent' };
            } else {
                return;
            }
        } else if (metric === 'tokens' && row.unit_type !== 'tokens') {
            return;
        } else if (metric === 'cost' && row.unit_type !== 'currency') {
            return;
        }
        sparklineData.push({
            provider_id: entry.provider_id,
            service_name: entry.service_name,
            timestamp: entry.timestamp,
            used_value: entry.used_value,
            limit_value: entry.limit_value,
            unit_type: entry.unit_type,
            window_type: entry.window_type
        });
    });
    if (stripEl) stripEl.innerHTML = buildProviderSparklineStrip(sparklineData, historyState.activeProviders, historyState.days);

    // Update charts with the same adapted data, filtered by active providers
    if (!skipChartUpdate) {
        const chartData = historyState.activeProviders
            ? sparklineData.filter(s => historyState.activeProviders.has(s.provider_id))
            : sparklineData;
        updateCharts(chartData, historyState.metric, historyState.days, historyState.windowFilter, historyState.showPeaks);
    }

    const container = document.getElementById('history-content');
    if (!history || history.length === 0) {
        container.innerHTML = '<p class="ht-empty">No history data found.</p>';
        return;
    }

    // Filter by provider if active
    let filtered = history;
    if (historyState.activeProviders) {
        filtered = history.filter(s => historyState.activeProviders.has(s.provider_id));
    }

    // Adapt each row to the active metric (converts GitHub requests → percent when applicable,
    // drops entries whose unit can't match the metric). Rows with nothing left are hidden.
    const metric = historyState.metric;
    let tableData = filtered
        .map(s => ({
            ...s,
            session: adaptEntryToMetric(s.session, metric),
            weekly: adaptEntryToMetric(s.weekly, metric),
            additional: (s.additional || [])
                .map(a => adaptEntryToMetric(a, metric))
                .filter(Boolean),
        }))
        .filter(s => s.session || s.weekly || s.additional.length > 0);

    // Apply window filter (session/weekly)
    if (historyState.windowFilter !== 'all') {
        tableData = tableData.filter(s => {
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

    const daysLabel = historyState.days >= 30 ? '30d' : historyState.days >= 7 ? '7d' : historyState.days >= 1 ? '24h' : '6h';
    const metaEl = document.getElementById('history-table-meta');
    if (metaEl) metaEl.textContent = `${totalItems.toLocaleString()} rows · last ${daysLabel}`;

    let html = `<table>
        <thead>
            <tr>
                <th>Time</th>
                <th>Provider</th>
                <th>Account</th>
                <th class="num">Session</th>
                <th class="num">Weekly</th>
                <th>Additional</th>
            </tr>
        </thead>
        <tbody>`;
    pageData.forEach(s => {
        const date = new Date(s.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        const session = formatWindowValue(s.session);
        const weekly = formatWindowValue(s.weekly);

        html += `<tr>
            <td class="ht-time">${date}</td>
            <td>${escapeHTML(s.provider_id || '—')}</td>
            <td class="ht-italic">${escapeHTML(s.account_label || '—')}</td>
            <td class="num ht-bold">${session}</td>
            <td class="num ht-bold">${weekly}</td>
            <td>${renderAdditional(s.additional.length ? s.additional : null)}</td>
        </tr>`;
    });
    html += '</tbody></table>';

    if (totalPages > 1) {
        html += `<div class="ht-pager">
            <div class="ht-pager-info">Showing ${start + 1}–${Math.min(start + pageSize, totalItems)} of ${totalItems}</div>
            <div class="ht-pager-nav">
                <button class="toggle-btn" ${historyState.page <= 1 ? 'disabled' : ''} onclick="setHistoryPage(${historyState.page - 1})">Previous</button>
                <div class="ht-pager-num">${historyState.page}<span>/</span>${totalPages}</div>
                <button class="toggle-btn" ${historyState.page >= totalPages ? 'disabled' : ''} onclick="setHistoryPage(${historyState.page + 1})">Next</button>
            </div>
        </div>`;
    }

    container.innerHTML = html;
}

export function setHistoryPage(page) {
    historyState.page = page;
    renderHistoryFromCache(true);
}

function renderHistoryFilterPill() {
    const pillEl = document.getElementById('history-filter-pill');
    if (!pillEl) return;
    const f = STATE.activeFilter;
    if (!f || !f.value) {
        pillEl.classList.add('hidden');
        pillEl.innerHTML = '';
        return;
    }
    pillEl.classList.remove('hidden');
    pillEl.innerHTML = `<span class="pill" style="cursor:default;border-style:dashed;">filter</span>
        <span class="pill pill-active" style="margin-left:4px;">${escapeHTML(f.value)}</span>
        <button class="pill" onclick="clearHistoryFilter()" style="margin-left:4px;">✕ clear</button>`;
}

export function clearHistoryFilter() {
    STATE.activeFilter = null;
    localStorage.removeItem('runway_active_filter');
    renderHistoryFromCache();
}

export async function loadHistoryView() {
    updateCsvHref();
    const container = document.getElementById('history-content');
    if (container) container.innerHTML = '<p class="ht-empty">Loading history…</p>';

    // Apply cross-view filter: if a provider_id filter is active from the dashboard, pre-select it
    const f = STATE.activeFilter;
    if (f && f.dimension === 'provider_id' && f.value) {
        historyState.activeProviders = new Set([f.value]);
    }

    try {
        // Fetch grouped data for table
        const response = await fetchHistoryCached({ days: historyState.days, limit: 1000 });
        _historyCache = response?.averages || [];

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
        if (container) container.innerHTML = `<p class="ht-empty" style="color:var(--crit);">Failed to load history: ${escapeHTML(err.message)}</p>`;
    }
}

export function initHistoryView() {
    window.setHistoryDays = setHistoryDays;
    window.setHistoryRange = setHistoryRange;
    window.setHistoryMetric = setHistoryMetric;
    window.setHistoryWindow = setHistoryWindow;
    window.setHistoryPeak = setHistoryPeak;
    window.toggleHistoryProvider = toggleHistoryProvider;
    window.setHistoryProvidersAll = setHistoryProvidersAll;
    window.setHistoryProvidersNone = setHistoryProvidersNone;
    window.setHistoryPage = setHistoryPage;
    window.clearHistoryFilter = clearHistoryFilter;
}