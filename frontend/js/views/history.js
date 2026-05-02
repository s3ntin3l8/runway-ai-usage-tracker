import { fetchHistory, fetchHistoryRaw, fetchHistoryDeltas } from '../api.js';
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
let _deltasCache = null;

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
    const windows = row.windows || [];
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
    if (entry?.model_id) {
        if (MODEL_LABEL_OVERRIDES[entry.model_id]) return MODEL_LABEL_OVERRIDES[entry.model_id];
        return entry.model_id;
    }
    const w = entry?.window || entry?.category;
    if (!w) return '—';
    return w.replace(/^seven_day_/, '').replace(/_/g, ' ');
}

function renderWindowsTable(row, metric) {
    const windows = row.windows || [];
    if (windows.length === 0) return '';

    const hasCost = windows.some(w => w.unit === 'currency');
    const hasTokens = windows.some(w => w.token_usage?.total != null);
    const hasMsgs = windows.some(w => w.msgs != null);

    let html = '<div class="ht-section-title">Windows & Breakdowns</div>';
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

        // Primary window row
        html += `<tr class="ht-window-primary">`;
        html += `<td>${windowLabel}</td>`;
        html += `<td>${modelLabel}</td>`;
        html += `<td class="num">${pct}</td>`;
        if (hasTokens) html += `<td class="num">${tokens}</td>`;
        if (hasCost) html += `<td class="num">${cost}</td>`;
        if (hasMsgs) html += `<td class="num">${msgs}</td>`;
        html += `</tr>`;

        // If this window has its own model breakdown, render it indented
        if (w.by_model && Object.keys(w.by_model).length > 0) {
            const models = Object.values(w.by_model);
            models.forEach(m => {
                html += `<tr class="ht-window-model-breakdown">`;
                html += `<td colspan="2" class="ht-model-name">└ ${escapeHTML(m.model_id)}</td>`;
                html += `<td></td>`; // Empty pct
                if (hasTokens) {
                    html += `<td class="num">${m.tokens_total != null ? formatValue(m.tokens_total, 'tokens') : '—'}</td>`;
                }
                if (hasCost) {
                    html += `<td class="num">${m.cost != null ? formatValue(m.cost, 'currency') : '—'}</td>`;
                }
                if (hasMsgs) {
                    html += `<td class="num">${m.msgs != null ? m.msgs.toLocaleString() : '—'}</td>`;
                }
                html += `</tr>`;
            });
        }
    });

    html += '</tbody></table>';
    return html;
}

function seriesKey(r) {
    return `${r.provider_id || ''}|${r.account_id || ''}|${r.window_type || ''}|${r.model_id || ''}|${r.unit_type || ''}`;
}

function groupBySeries(rows) {
    const groups = new Map();
    rows.forEach(r => {
        const key = seriesKey(r);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(r);
    });
    groups.forEach(arr => arr.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp)));
    return groups;
}

function positiveTokenDeltas(seriesRows) {
    if (seriesRows.length === 0) return 0;
    let total = 0;
    let firstVal = seriesRows[0].token_usage?.total ?? seriesRows[0].used_value ?? 0;
    let maxSeen = firstVal;
    const GLITCH_THRESHOLD = 0.5;

    for (let i = 1; i < seriesRows.length; i++) {
        const curr = seriesRows[i].token_usage?.total ?? seriesRows[i].used_value ?? 0;
        
        // Baseline read: if we were at 0 and jump to a value, treat as baseline
        if (maxSeen === 0 && curr > 0) {
            maxSeen = curr;
            continue;
        }

        if (curr > maxSeen) {
            total += curr - maxSeen;
            maxSeen = curr;
        } else if (curr < maxSeen * GLITCH_THRESHOLD) {
            maxSeen = curr;
        }
    }
    return total;
}

function positiveCurrencyDeltas(seriesRows) {
    if (seriesRows.length === 0) return 0;
    let total = 0;
    let firstVal = seriesRows[0].used_value ?? 0;
    let maxSeen = firstVal;
    const GLITCH_THRESHOLD = 0.5;

    for (let i = 1; i < seriesRows.length; i++) {
        const curr = seriesRows[i].used_value ?? 0;

        // Baseline read: if we were at 0 and jump to a value, treat as baseline
        if (maxSeen === 0 && curr > 0) {
            maxSeen = curr;
            continue;
        }

        if (curr > maxSeen) {
            total += curr - maxSeen;
            maxSeen = curr;
        } else if (curr < maxSeen * GLITCH_THRESHOLD) {
            maxSeen = curr;
        }
    }
    return total;
}

function hasCriticalReading(seriesRows) {
    return seriesRows.some(r => {
        if (r.used_value == null) return false;
        if (r.unit_type === 'percent') return r.used_value >= 90;
        if (r.limit_value > 0) return (r.used_value / r.limit_value) >= 0.9;
        return false;
    });
}

function renderHistoryTiles(rawHistory, metric, days, deltas) {
    const container = document.getElementById('history-tiles');
    if (!container) return;

    const minutes = days * 24 * 60;
    const providerNames = {
        claude: 'Claude', chatgpt: 'ChatGPT', gemini: 'Gemini',
        copilot: 'Copilot', opencode: 'Opencode', zai: 'Z.AI',
        ollama: 'Ollama', openrouter: 'OpenRouter', kimi: 'Kimi',
        minimax: 'MiniMax', anthropic: 'Claude', openai: 'ChatGPT',
        github: 'Copilot',
    };

    // ------------------------------------------------------------------
    // Prefer server-computed deltas; fall back to client-side computation
    // ------------------------------------------------------------------
    let totalTokenDelta = 0;
    let totalCostDelta = 0;
    let providerTokenDeltas = {};
    let critSeries = 0;
    let sampled = false;

    if (deltas && typeof deltas.token_delta_total === 'number') {
        totalTokenDelta = deltas.token_delta_total;
        totalCostDelta = deltas.cost_delta_total;
        providerTokenDeltas = deltas.provider_token_deltas || {};
        critSeries = deltas.critical_series_count || 0;
        sampled = deltas.series_sampled || false;
    } else {
        // Client-side fallback (for when deltas endpoint fails or is absent)
        let rows = rawHistory || [];
        if (historyState.activeProviders) {
            rows = rows.filter(r => historyState.activeProviders.has(r.provider_id));
        }
        if (rows.length === 0) {
            container.innerHTML = '<div class="hud-panel tile" style="grid-column:1/-1;"><div class="t-kicker">No data</div><div class="t-val">—</div></div>';
            return;
        }
        const series = groupBySeries(rows);
        const seriesDeltas = new Map();

        series.forEach((arr, key) => {
            const tokenDelta = positiveTokenDeltas(arr);
            const costDelta = arr[0]?.unit_type === 'currency' ? positiveCurrencyDeltas(arr) : 0;
            const critical = hasCriticalReading(arr);
            const r = arr[0];

            seriesDeltas.set(key, {
                tokenDelta,
                costDelta,
                critical,
                providerId: r.provider_id,
                accountId: r.account_id,
                windowType: r.window_type,
                modelId: r.model_id,
                unitType: r.unit_type,
            });
        });

        // Hierarchy Filter (Prevent double-counting)
        const hierarchyGroups = new Map();
        seriesDeltas.forEach((d, key) => {
            const hKey = `${d.providerId}|${d.accountId}|${d.windowType}|${d.unitType}`;
            if (!hierarchyGroups.has(hKey)) hierarchyGroups.set(hKey, []);
            hierarchyGroups.get(hKey).push(key);
        });

        hierarchyGroups.forEach((keys, hKey) => {
            const modelKeys = keys.filter(k => seriesDeltas.get(k).modelId != null);
            const selectedKeys = modelKeys.length > 0 ? modelKeys : keys;

            selectedKeys.forEach(k => {
                const d = seriesDeltas.get(k);
                totalTokenDelta += d.tokenDelta;
                totalCostDelta += d.costDelta;
                providerTokenDeltas[d.providerId] = (providerTokenDeltas[d.providerId] || 0) + d.tokenDelta;
                if (d.critical) critSeries++;
            });
        });
    }

    if (totalTokenDelta === 0 && totalCostDelta === 0 && critSeries === 0 && Object.keys(providerTokenDeltas).length === 0) {
        container.innerHTML = '<div class="hud-panel tile" style="grid-column:1/-1;"><div class="t-kicker">No data</div><div class="t-val">—</div></div>';
        return;
    }

    const burnRate = minutes > 0 ? totalTokenDelta / minutes : 0;
    let burnLabel = `${Math.round(burnRate)}<span>tok/min</span>`;
    if (burnRate >= 1_000_000) {
        burnLabel = `${(burnRate / 1_000_000).toFixed(1)}<span>M tok/min</span>`;
    } else if (burnRate >= 1000) {
        burnLabel = `${(burnRate / 1000).toFixed(1)}<span>k tok/min</span>`;
    }

    // Sparkline: always computed client-side from rawHistory for visual shape
    let sparklineSvg = '';
    if (rawHistory && rawHistory.length > 1) {
        const bucketCount = 12;
        const sorted = [...rawHistory].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
        const timeSpan = new Date(sorted[sorted.length - 1].timestamp) - new Date(sorted[0].timestamp);
        const bucketMs = timeSpan / bucketCount || 1;
        const buckets = new Array(bucketCount).fill(0);
        sorted.forEach(r => {
            const t = new Date(r.timestamp) - new Date(sorted[0].timestamp);
            const idx = Math.min(bucketCount - 1, Math.floor(t / bucketMs));
            buckets[idx] += (r.token_usage?.total || 0);
        });
        const maxBucket = Math.max(...buckets, 1);
        const w = 120, h = 28;
        const step = w / (bucketCount - 1);
        const points = buckets.map((v, i) => {
            const x = i * step;
            const y = h - (v / maxBucket) * h;
            return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
        sparklineSvg = `<svg viewBox="0 0 ${w} ${h}" class="t-spark" preserveAspectRatio="none"><polyline points="${points}" fill="none" stroke="var(--accent)" stroke-width="1.6"/></svg>`;
    }

    // Hottest provider
    const providerEntries = Object.entries(providerTokenDeltas).sort((a, b) => b[1] - a[1]);
    const [hotProvider, hotTokens] = providerEntries[0] || ['—', 0];
    const hotShare = totalTokenDelta > 0 ? Math.round((hotTokens / totalTokenDelta) * 100) : 0;
    const hotName = providerNames[hotProvider] || hotProvider;

    let html = '';

    html += `<div class="hud-panel tile">
        <div class="t-kicker">Burn rate · avg</div>
        <div class="t-val">${burnLabel}</div>
        ${sparklineSvg}
    </div>`;

    html += `<div class="hud-panel tile">
        <div class="t-kicker">Est. cost · period</div>
        <div class="t-val">$${totalCostDelta.toFixed(2)}<span>spent</span></div>
    </div>`;

    html += `<div class="hud-panel tile">
        <div class="t-kicker">Hottest provider</div>
        <div class="t-val" style="font-size:22px">${escapeHTML(hotName)}</div>
        <div class="t-sub"><b>${Math.round(hotTokens).toLocaleString()} tok</b> · ${hotShare}% share</div>
    </div>`;

    html += `<div class="hud-panel tile">
        <div class="t-kicker">Critical events</div>
        <div class="t-val">${critSeries}<span>series</span>${sampled ? ' <span style="font-size:11px;color:var(--text-dim)">*</span>' : ''}</div>
        <div class="t-sub">${critSeries > 0 ? '≥90% limit crossed' : 'all clear'}${sampled ? ' · partial sample' : ''}</div>
    </div>`;

    container.innerHTML = html;
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

    // Update summary tiles
    renderHistoryTiles(rawHistory, historyState.metric, historyState.days, _deltasCache);

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
    let tableData = filtered.filter(s => {
        const windows = s.windows || [];
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

    // Apply window filter (session/weekly/monthly)
    if (historyState.windowFilter !== 'all') {
        tableData = tableData.filter(s => {
            const windows = s.windows || [];
            return windows.some(w => w.category === historyState.windowFilter);
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
        // Fetch grouped data for table, raw data for chart, and deltas for tiles
        const [response, rawResponse, deltasResponse] = await Promise.all([
            fetchHistoryCached({ days: historyState.days, limit: 1000 }),
            fetchHistoryRaw({ days: historyState.days, limit: 1000 }).catch(rawErr => {
                console.warn('Failed to fetch raw history for chart:', rawErr);
                return [];
            }),
            fetchHistoryDeltas({ days: historyState.days }).catch(deltasErr => {
                console.warn('Failed to fetch history deltas:', deltasErr);
                return null;
            }),
        ]);

        _historyCache = response?.averages || [];
        _historyRawCache = rawResponse || [];
        _deltasCache = deltasResponse;

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