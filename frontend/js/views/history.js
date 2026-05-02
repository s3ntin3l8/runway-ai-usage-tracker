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

const _expandedRows = new Set(); // Set of row index strings

function toggleExpandRow(rowIndex) {
    const el = document.getElementById(`ht-detail-${rowIndex}`);
    const btn = document.getElementById(`ht-expand-${rowIndex}`);
    if (!el || !btn) return;
    const isOpen = el.classList.contains('open');
    if (isOpen) {
        el.classList.remove('open');
        btn.classList.remove('expanded');
        _expandedRows.delete(rowIndex);
    } else {
        el.classList.add('open');
        btn.classList.add('expanded');
        _expandedRows.add(rowIndex);
    }
}

function computePrimaryValue(row, metric) {
    const windows = [row.session, row.weekly, ...(row.additional || [])].filter(Boolean);
    if (windows.length === 0) return null;

    if (metric === 'percent') {
        const pcts = windows.map(w => {
            if (w.unit === 'percent') return w.value;
            if (w.limit && w.limit > 0) return (w.value / w.limit) * 100;
            return null;
        }).filter(v => v != null);
        return pcts.length > 0 ? Math.max(...pcts) : null;
    }

    if (metric === 'tokens') {
        const total = windows.reduce((sum, w) => sum + (w.token_usage?.total || 0), 0);
        return total > 0 ? total : null;
    }

    if (metric === 'cost') {
        const total = windows
            .filter(w => w.unit === 'currency')
            .reduce((sum, w) => sum + (w.value || 0), 0);
        return total > 0 ? total : null;
    }

    return null;
}

function primaryColumnHeader(metric) {
    if (metric === 'percent') return 'Used %';
    if (metric === 'tokens') return 'Tokens';
    if (metric === 'cost') return 'Cost';
    return 'Value';
}

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

    const daysLabel = days >= 30 ? '30D' : days >= 7 ? '7D' : days >= 1 ? '24H' : '6H';
    const providerCount = new Set(rows.map(r => r.provider_id)).size;

    if (metric === 'tokens') {
        const tokenRows = rows.filter(r => r.token_usage?.total != null).map(r => r.token_usage.total);
        if (tokenRows.length === 0) return '';
        const total = tokenRows.reduce((s, v) => s + v, 0);
        const avg = total / tokenRows.length;
        const peak = Math.max(...tokenRows);
        return `Showing ${daysLabel} · ${providerCount} provider${providerCount !== 1 ? 's' : ''} · avg ${Math.round(avg).toLocaleString()} tokens · peak ${Math.round(peak).toLocaleString()} tokens · total ${Math.round(total).toLocaleString()}`;
    }

    if (metric === 'cost') {
        const costRows = rows.filter(r => r.unit_type === 'currency' && r.used_value != null).map(r => r.used_value);
        if (costRows.length === 0) return '';
        const total = costRows.reduce((s, v) => s + v, 0);
        return `Showing ${daysLabel} · ${providerCount} provider${providerCount !== 1 ? 's' : ''} · total $${total.toFixed(2)}`;
    }

    // Compute from percent-compatible rows only
    const pctRows = rows.filter(r => {
        if (r.used_value == null) return false;
        return r.unit_type === 'percent' || (r.limit_value > 0);
    }).map(r => {
        return r.unit_type === 'percent' ? r.used_value : (r.used_value / r.limit_value) * 100;
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

    let parts = [`Showing ${daysLabel} · ${providerCount} provider${providerCount !== 1 ? 's' : ''} · avg ${avg.toFixed(1)}% · peak ${peak.toFixed(1)}%`];
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
    if (unitStr === 'tokens') return Math.round(value).toLocaleString();
    if (unitStr === 'requests') return `${value.toLocaleString()} requests`;
    return `${value.toLocaleString()}${unitStr}`;
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

function renderWindowsTable(row, metric) {
    const windows = [
        row.session ? { ...row.session, window: 'session' } : null,
        row.weekly ? { ...row.weekly, window: 'weekly' } : null,
        ...(row.additional || []).map(a => ({ ...a, window: a.window || 'other' })),
    ].filter(Boolean);

    if (windows.length === 0) return '';

    const hasCost = windows.some(w => w.unit === 'currency');
    const hasTokens = windows.some(w => w.token_usage?.total != null);
    const hasMsgs = windows.some(w => w.msgs != null);

    let html = '<div class="ht-section-title">Windows</div>';
    html += '<table class="ht-detail-table"><thead><tr>';
    html += '<th>Window</th><th>Model</th>';
    html += '<th class="num">Used %</th>';
    if (hasTokens) html += '<th class="num">Tokens</th>';
    if (hasCost) html += '<th class="num">Cost</th>';
    if (hasMsgs) html += '<th class="num">Msgs</th>';
    html += '</tr></thead><tbody>';

    windows.forEach(w => {
        const windowLabel = escapeHTML(w.window);
        const modelLabel = w.model_id ? escapeHTML(friendlyWindowLabel({ model_id: w.model_id })) : '—';

        let pct = '—';
        if (w.unit === 'percent') {
            pct = formatValue(w.value, 'percent');
        } else if (w.limit && w.limit > 0) {
            pct = formatValue((w.value / w.limit) * 100, 'percent');
        }

        const tokens = w.token_usage?.total != null ? formatValue(w.token_usage.total, 'tokens') : '—';
        const cost = w.unit === 'currency' && w.value != null ? formatValue(w.value, 'currency') : '—';
        const msgs = w.msgs != null ? w.msgs.toLocaleString() : '—';

        html += `<tr>`;
        html += `<td>${windowLabel}</td>`;
        html += `<td>${modelLabel}</td>`;
        html += `<td class="num">${pct}</td>`;
        if (hasTokens) html += `<td class="num">${tokens}</td>`;
        if (hasCost) html += `<td class="num">${cost}</td>`;
        if (hasMsgs) html += `<td class="num">${msgs}</td>`;
        html += `</tr>`;
    });

    html += '</tbody></table>';
    return html;
}

function renderModelBreakdown(byModel) {
    if (!byModel || byModel.length === 0) return '';

    const hasCost = byModel.some(m => m.cost != null);
    const hasMsgs = byModel.some(m => m.msgs != null);
    const hasTokens = byModel.some(m => m.tokens_total != null);

    let html = '<div class="ht-section-title">Model Breakdown</div>';
    html += '<table class="ht-detail-table"><thead><tr>';
    html += '<th>Model</th>';
    if (hasCost) html += '<th class="num">Cost</th>';
    if (hasMsgs) html += '<th class="num">Msgs</th>';
    if (hasTokens) html += '<th class="num">Input</th><th class="num">Output</th><th class="num">Total</th>';
    html += '</tr></thead><tbody>';

    byModel.forEach(m => {
        html += `<tr>`;
        html += `<td>${escapeHTML(m.model_id)}</td>`;
        if (hasCost) html += `<td class="num">${m.cost != null ? formatValue(m.cost, 'currency') : '—'}</td>`;
        if (hasMsgs) html += `<td class="num">${m.msgs != null ? m.msgs.toLocaleString() : '—'}</td>`;
        if (hasTokens) {
            html += `<td class="num">${m.tokens_input != null ? formatValue(m.tokens_input, 'tokens') : '—'}</td>`;
            html += `<td class="num">${m.tokens_output != null ? formatValue(m.tokens_output, 'tokens') : '—'}</td>`;
            html += `<td class="num">${m.tokens_total != null ? formatValue(m.tokens_total, 'tokens') : '—'}</td>`;
        }
        html += `</tr>`;
    });

    html += '</tbody></table>';
    return html;
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
        const metric = historyState.metric;
        let value = row.used_value;
        let unit_type = row.unit_type;

        if (metric === 'percent') {
            if (row.unit_type === 'percent') {
                value = row.used_value;
            } else if (row.limit_value && row.limit_value > 0) {
                value = (row.used_value / row.limit_value) * 100;
                unit_type = 'percent';
            } else {
                return;
            }
        } else if (metric === 'tokens') {
            // Use token_usage.total from backend (new format)
            if (row.token_usage?.total != null) {
                value = row.token_usage.total;
                unit_type = 'tokens';
            } else if (row.unit_type === 'tokens') {
                value = row.used_value;
            } else {
                return;
            }
        } else if (metric === 'cost') {
            if (row.unit_type !== 'currency') return;
        }

        if (value == null) return;
        sparklineData.push({
            provider_id: row.provider_id,
            service_name: row.service_name,
            timestamp: row.timestamp,
            used_value: value,
            limit_value: row.limit_value,
            unit_type: unit_type,
            window_type: row.window_type,
            token_usage: row.token_usage,
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

    // Filter: keep rows that have at least one window with data for the active metric
    const metric = historyState.metric;
    const tableData = filtered.filter(s => {
        const windows = [s.session, s.weekly, ...(s.additional || [])].filter(Boolean);
        if (windows.length === 0) return false;
        if (metric === 'percent') {
            return windows.some(w => w.unit === 'percent' || (w.limit && w.limit > 0));
        }
        if (metric === 'tokens') {
            return windows.some(w => w.token_usage?.total != null || w.unit === 'tokens');
        }
        if (metric === 'cost') {
            return windows.some(w => w.unit === 'currency');
        }
        return true;
    });

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

    const metricLabel = primaryColumnHeader(metric);

    let html = `<table>
        <thead>
            <tr>
                <th></th>
                <th>Time</th>
                <th>Provider</th>
                <th>Account</th>
                <th class="num">${escapeHTML(metricLabel)}</th>
            </tr>
        </thead>
        <tbody>`;

    pageData.forEach((s, idx) => {
        const rowIndex = `${historyState.page}-${idx}`;
        const date = new Date(s.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        const primary = computePrimaryValue(s, metric);
        const primaryVal = primary != null ? formatValue(primary, metric === 'percent' ? 'percent' : metric === 'tokens' ? 'tokens' : 'currency') : '—';
        const isExpanded = _expandedRows.has(rowIndex);

        // Collapsed row
        html += `<tr>
            <td style="width:24px;padding-right:0;">
                <button id="ht-expand-${rowIndex}" class="ht-expand ${isExpanded ? 'expanded' : ''}" onclick="toggleExpandRow('${rowIndex}')">▶</button>
            </td>
            <td class="ht-time">${date}</td>
            <td>${escapeHTML(s.provider_id || '—')}</td>
            <td class="ht-italic">${escapeHTML(s.account_label || '—')}</td>
            <td class="num ht-bold">${primaryVal}</td>
        </tr>`;

        // Expanded detail row
        html += `<tr id="ht-detail-${rowIndex}" class="ht-detail-row ${isExpanded ? 'open' : ''}">
            <td colspan="5">
                <div class="ht-detail-inner">
                    ${renderWindowsTable(s, metric)}
                    ${renderModelBreakdown(s.by_model)}
                </div>
            </td>
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
    window.toggleExpandRow = toggleExpandRow;
}