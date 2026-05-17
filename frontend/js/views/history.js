import { fetchHistoryDeltas } from '../api.js';
import { buildProviderSparklineStrip } from '../components.js';
import { updateCharts, destroyCharts } from '../charts.js';
import { STATE } from '../state.js';
import { formatLocalDate, formatLocalDateTime } from '../utils/tz.js';
import { escapeHTML } from '../utils/html.js';

const historyState = {
    days: 1,
    activeProviders: null,       // Set of provider_ids, or null = all
    metric: 'percent',           // 'percent' | 'tokens' | 'cost'
    windowFilter: 'all',         // 'all' | 'session' | 'daily' | 'weekly' | 'monthly'
    page: 1,
    _windowsCache: null,
    _chartCache: null,
    _deltasCache: null,
};

// Short alias kept for terser template-literal interpolation.
const escHtml = escapeHTML;

export function getHistoryState() {
    return historyState;
}

// ---------------------------------------------------------------------------
// Control setters
// ---------------------------------------------------------------------------

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
    loadHistoryView();
}

export function setHistoryWindow(windowType) {
    historyState.windowFilter = windowType;
    historyState.page = 1;
    document.querySelectorAll('#history-window-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.window === windowType);
    });
    loadHistoryView();
}

export function toggleHistoryProvider(pid) {
    historyState.page = 1;
    const known = _knownProviderIds();
    if (!historyState.activeProviders) {
        historyState.activeProviders = new Set(known);
        historyState.activeProviders.delete(pid);
    } else if (historyState.activeProviders.has(pid)) {
        historyState.activeProviders.delete(pid);
    } else {
        historyState.activeProviders.add(pid);
    }
    if (known.length > 0 && known.every(p => historyState.activeProviders.has(p))) {
        historyState.activeProviders = null;
    }
    updateCsvHref();
    loadHistoryView();
}

function _knownProviderIds() {
    const cache = historyState._chartCache;
    if (!cache) return [];
    const ids = new Set();
    for (const s of (cache.series || [])) if (s.provider_id) ids.add(s.provider_id);
    for (const bar of (cache.bars || []))
        for (const seg of (bar.segments || []))
            if (seg.provider_id) ids.add(seg.provider_id);
    return [...ids];
}

export function setHistoryProvidersAll() {
    historyState.activeProviders = null;
    historyState.page = 1;
    updateCsvHref();
    loadHistoryView();
}

export function setHistoryProvidersNone() {
    historyState.activeProviders = new Set();
    historyState.page = 1;
    updateCsvHref();
    loadHistoryView();
}

// ---------------------------------------------------------------------------
// Fetch functions
// ---------------------------------------------------------------------------

async function fetchHistorySnapshots() {
    const params = new URLSearchParams({ days: historyState.days, page: historyState.page, limit: 50 });
    if (historyState.activeProviders?.size === 1)
        params.set('provider_id', [...historyState.activeProviders][0]);
    if (historyState.windowFilter !== 'all')
        params.set('window_type', historyState.windowFilter);
    const r = await fetch(`/api/v1/usage/history/snapshots?${params}`);
    if (!r.ok) throw new Error(`snapshots ${r.status}`);
    return r.json();
}

async function fetchHistoryChart() {
    const params = new URLSearchParams({ days: historyState.days, metric: historyState.metric });
    // Never filter by provider at API level — chart cache must contain all providers
    // so the sparkline strip can always render every pill (client-side filtering handles the chart).
    const r = await fetch(`/api/v1/usage/history/chart?${params}`);
    if (!r.ok) throw new Error(`chart ${r.status}`);
    return r.json();
}

// ---------------------------------------------------------------------------
// Misc helpers
// ---------------------------------------------------------------------------

function updateCsvHref() {
    const btn = document.getElementById('csv-download-btn');
    if (!btn) return;
    const params = new URLSearchParams({ format: 'csv', days: historyState.days });
    if (historyState.activeProviders && historyState.activeProviders.size === 1) {
        params.set('provider_id', [...historyState.activeProviders][0]);
    }
    btn.href = `/api/v1/usage/history?${params.toString()}`;
}

// ---------------------------------------------------------------------------
// Summary tiles (uses deltas data)
// ---------------------------------------------------------------------------

function groupBySeries(rows) {
    const groups = new Map();
    rows.forEach(r => {
        const key = `${r.provider_id || ''}|${r.account_id || ''}|${r.window_type || ''}|${r.model_id || ''}|${r.unit_type || ''}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(r);
    });
    groups.forEach(arr => arr.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp)));
    return groups;
}

function positiveTokenDeltas(seriesRows) {
    if (seriesRows.length === 0) return 0;
    let total = 0;
    let maxSeen = seriesRows[0].token_usage?.total ?? seriesRows[0].used_value ?? 0;
    const GLITCH_THRESHOLD = 0.5;
    for (let i = 1; i < seriesRows.length; i++) {
        const curr = seriesRows[i].token_usage?.total ?? seriesRows[i].used_value ?? 0;
        if (maxSeen === 0 && curr > 0) { maxSeen = curr; continue; }
        if (curr > maxSeen) { total += curr - maxSeen; maxSeen = curr; }
        else if (curr < maxSeen * GLITCH_THRESHOLD) { maxSeen = curr; }
    }
    return total;
}

function positiveCurrencyDeltas(seriesRows) {
    if (seriesRows.length === 0) return 0;
    let total = 0;
    let maxSeen = seriesRows[0].used_value ?? 0;
    const GLITCH_THRESHOLD = 0.5;
    for (let i = 1; i < seriesRows.length; i++) {
        const curr = seriesRows[i].used_value ?? 0;
        if (maxSeen === 0 && curr > 0) { maxSeen = curr; continue; }
        if (curr > maxSeen) { total += curr - maxSeen; maxSeen = curr; }
        else if (curr < maxSeen * GLITCH_THRESHOLD) { maxSeen = curr; }
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

function renderHistoryTiles(deltas) {
    const container = document.getElementById('history-tiles');
    if (!container) return;

    const minutes = historyState.days * 24 * 60;
    const providerNames = {
        claude: 'Claude', chatgpt: 'ChatGPT', gemini: 'Gemini',
        copilot: 'Copilot', opencode: 'Opencode', zai: 'Z.AI',
        ollama: 'Ollama', openrouter: 'OpenRouter', kimi: 'Kimi',
        minimax: 'MiniMax', anthropic: 'Claude', openai: 'ChatGPT',
        github: 'Copilot',
    };

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

    const rangeLabel = ({ 0.042: '1h', 0.25: '6h', 1: '24h', 7: '7d', 30: '30d', 90: 'all' })[historyState.days] || `${historyState.days}d`;

    const providerEntries = Object.entries(providerTokenDeltas).sort((a, b) => b[1] - a[1]);
    const [hotProvider, hotTokens] = providerEntries[0] || ['—', 0];
    const hotShare = totalTokenDelta > 0 ? Math.round((hotTokens / totalTokenDelta) * 100) : 0;
    const hotName = providerNames[hotProvider] || hotProvider;

    let html = '';
    html += `<div class="hud-panel tile">
        <div class="t-kicker">Burn rate · avg</div>
        <div class="t-val">${burnLabel}</div>
    </div>`;
    html += `<div class="hud-panel tile">
        <div class="t-kicker">Est. cost · ${rangeLabel}</div>
        <div class="t-val">$${totalCostDelta.toFixed(2)}<span>spent</span></div>
    </div>`;
    html += `<div class="hud-panel tile">
        <div class="t-kicker">Hottest · ${rangeLabel}</div>
        <div class="t-val" style="font-size:22px">${escHtml(hotName)}</div>
        <div class="t-sub"><b>${Math.round(hotTokens).toLocaleString()} tok</b> · ${hotShare}% share</div>
    </div>`;
    html += `<div class="hud-panel tile">
        <div class="t-kicker">Critical events</div>
        <div class="t-val">${critSeries}<span>series</span>${sampled ? ' <span style="font-size:11px;color:var(--text-dim)">*</span>' : ''}</div>
        <div class="t-sub">${critSeries > 0 ? '≥90% limit crossed' : 'all clear'}${sampled ? ' · partial sample' : ''}</div>
    </div>`;

    container.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Chart rendering — converts new API format to old Chart.js snapshots format
// ---------------------------------------------------------------------------

function _seriesPercent(series) {
    // percent series → [{provider_id, service_name, timestamp, used_value, limit_value, unit_type, window_type}]
    const rows = [];
    for (const s of series) {
        for (const p of s.points) {
            rows.push({
                provider_id: s.provider_id,
                service_name: s.label,
                timestamp: p.ts,
                used_value: p.pct_used,
                limit_value: 100,
                unit_type: 'percent',
                window_type: s.window_type,
                model_id: s.model_id,
                token_usage: null,
            });
        }
    }
    return rows;
}

function _barSnapshots(bars, metric) {
    // daily bars → [{provider_id, service_name, timestamp, used_value, unit_type, window_type, token_usage}]
    const rows = [];
    for (const bar of bars) {
        for (const seg of bar.segments) {
            rows.push({
                provider_id: seg.provider_id,
                service_name: seg.label,
                timestamp: bar.ts || (bar.date + 'T12:00:00Z'),
                used_value: seg.value,
                limit_value: null,
                unit_type: metric === 'cost' ? 'currency' : 'tokens',
                window_type: 'day',
                model_id: seg.model_id,
                token_usage: metric === 'tokens' ? { total: seg.value } : null,
            });
        }
    }
    return rows;
}

function renderHistoryChart() {
    const cache = historyState._chartCache;
    if (!cache) return;

    let snapshots;
    if (historyState.metric === 'percent') {
        snapshots = _seriesPercent(cache.series || []);
    } else {
        snapshots = _barSnapshots(cache.bars || [], historyState.metric);
    }

    // Filter by active providers
    const active = historyState.activeProviders;
    const filtered = active ? snapshots.filter(s => active.has(s.provider_id)) : snapshots;

    // Sparkline strip
    const stripEl = document.getElementById('history-sparkline-strip');
    if (stripEl) {
        stripEl.innerHTML = buildProviderSparklineStrip(snapshots, active, historyState.days);
    }

    updateCharts(filtered, historyState.metric, historyState.days, historyState.windowFilter, false);
}

// ---------------------------------------------------------------------------
// Snapshot table
// ---------------------------------------------------------------------------

function renderSnapshotTable() {
    const container = document.getElementById('history-content');
    if (!container) return;
    const cache = historyState._windowsCache;
    if (!cache) { container.innerHTML = '<p class="ht-empty">Loading…</p>'; return; }

    const allRows = cache.rows || [];
    const rows = allRows.filter(r => r.pct_used == null || r.pct_used > 0);
    if (!rows.length) {
        container.innerHTML = '<p class="ht-empty">No data for selected range.</p>';
        return;
    }

    function fmtTokens(n) {
        if (n == null) return '—';
        if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
        if (n >= 1e3) return Math.round(n / 1e3) + 'k';
        return String(n);
    }
    function fmtCost(n) {
        if (n == null || n === 0) return '—';
        return '$' + n.toFixed(2);
    }

    const seenSeries = new Set();
    const rowsHtml = rows.map(r => {
        const seriesKey = `${r.provider_id}|${r.account_id}|${r.window_type}|${r.model_id}`;
        const isLatest = !seenSeries.has(seriesKey);
        seenSeries.add(seriesKey);

        const fillBar = renderFillBar(r.pct_used);
        const pctStr = r.pct_used != null ? r.pct_used.toFixed(1) + '%' : '—';
        const deltaClass = r.delta > 0 ? 'hw-delta-up' : r.delta < 0 ? 'hw-delta-down' : '';
        const deltaStr = r.delta != null
            ? `<span class="hw-delta ${deltaClass}">${r.delta > 0 ? '+' : ''}${r.delta.toFixed(1)}%</span>`
            : '<span class="hw-delta">—</span>';
        const tsStr = formatLocalDateTime(r.ts, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        const liveBadge = isLatest ? ' <span class="hw-live-badge">LIVE</span>' : '';
        return `<tr class="hw-row">
          <td class="hw-ts-cell">${tsStr}</td>
          <td class="hw-type-cell"><span class="hw-window-badge">${escHtml(r.window_type)}</span></td>
          <td class="hw-provider-cell">${escHtml(r.service_name || r.provider_id)}${liveBadge}</td>
          <td class="hw-account-cell" title="${escHtml(r.account_id)}">${escHtml(r.account_id)}</td>
          <td class="hw-model-cell">${escHtml(r.model_label || '-')}</td>
          <td class="hw-metric-cell"><div class="hw-metric-inner">${fillBar}${escHtml(pctStr)}</div></td>
          <td class="hw-delta-cell">${deltaStr}</td>
          <td class="hw-tokens-cell">${fmtTokens(r.tokens_total)}</td>
          <td class="hw-cost-cell">${fmtCost(r.cost_usd)}</td>
        </tr>`;
    }).join('');

    const metaEl = document.getElementById('history-table-meta');
    if (metaEl) {
        const rangeLabel = { 0.042: '1h', 0.25: '6h', 1: '24h', 7: '7d', 30: '30d', 90: 'all' }[historyState.days]
            ?? `${historyState.days}d`;
        const hiddenCount = allRows.length - rows.length;
        const hiddenNote = hiddenCount > 0 ? ` · ${hiddenCount} zero-usage hidden` : '';
        metaEl.textContent = `${cache.total} snapshots · ${rangeLabel} · page ${cache.page}${hiddenNote}`;
    }

    container.innerHTML = `
      <table class="hw-table">
        <thead><tr>
          <th>TIME</th><th>TYPE</th><th>PROVIDER</th><th>ACCOUNT</th><th>MODEL</th><th>% USED</th><th>DELTA</th><th>TOKENS</th><th>COST</th>
        </tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
      ${renderHWPager(cache.total, cache.page)}`;
}

function renderFillBar(pctUsed) {
    if (pctUsed == null) return '';
    const pct = Math.min(100, Math.max(0, pctUsed));
    const color = pct >= 80 ? '#c0392b' : pct >= 50 ? '#d4a017' : '#27ae60';
    const fillW = (pct * 0.6).toFixed(1);
    const fillRect = pct > 0 ? `<rect x="0" y="2" width="${fillW}" height="6" rx="2" fill="${color}"/>` : '';
    return `<svg class="hw-fill-bar" width="60" height="10" viewBox="0 0 60 10">
      <rect x="0" y="2" width="60" height="6" rx="2" fill="#808080" fill-opacity="0.2"/>
      ${fillRect}
    </svg> `;
}

function renderHWPager(total, page) {
    const perPage = 50;
    if (total <= perPage) return '';
    const pages = Math.ceil(total / perPage);
    return `<div class="ht-pager">
      <div class="ht-pager-nav">
        <button class="toggle-btn" ${page <= 1 ? 'disabled' : ''} onclick="hwGoPage(${page - 1})">← Prev</button>
        <div class="ht-pager-num">${page}<span>/</span>${pages}</div>
        <button class="toggle-btn" ${page >= pages ? 'disabled' : ''} onclick="hwGoPage(${page + 1})">Next →</button>
      </div>
    </div>`;
}

export function hwGoPage(p) {
    historyState.page = p;
    fetchHistorySnapshots().then(data => {
        historyState._windowsCache = data;
        renderSnapshotTable();
    }).catch(e => console.warn('page fetch failed', e));
}

// ---------------------------------------------------------------------------
// Filter pill
// ---------------------------------------------------------------------------

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
      <span class="pill pill-active" style="margin-left:4px;">${escHtml(f.value)}</span>
      <button class="pill" onclick="clearHistoryFilter()" style="margin-left:4px;">✕ clear</button>`;
}

export function clearHistoryFilter() {
    STATE.activeFilter = null;
    localStorage.removeItem('runway_active_filter');
    loadHistoryView();
}

// ---------------------------------------------------------------------------
// Main orchestrator
// ---------------------------------------------------------------------------

export function renderHistoryFromCache() {
    renderHistoryTiles(historyState._deltasCache);
    renderHistoryFilterPill();
    renderHistoryChart();
    renderSnapshotTable();
}

export async function loadHistoryView() {
    updateCsvHref();
    const container = document.getElementById('history-content');
    if (container) container.innerHTML = '<p class="ht-empty">Loading…</p>';

    // Apply cross-view filter
    const f = STATE.activeFilter;
    if (f && f.dimension === 'provider_id' && f.value) {
        historyState.activeProviders = new Set([f.value]);
    }

    try {
        const [snapshots, chart, deltas] = await Promise.all([
            fetchHistorySnapshots(),
            fetchHistoryChart(),
            fetchHistoryDeltas({ days: historyState.days }).catch(e => {
                console.warn('deltas fetch failed', e);
                return null;
            }),
        ]);
        historyState._windowsCache = snapshots;
        historyState._chartCache = chart;
        historyState._deltasCache = deltas;
        renderHistoryFromCache();
    } catch (e) {
        console.error('history load failed', e);
        destroyCharts();
        if (container) container.innerHTML = `<p class="ht-empty" style="color:var(--crit);">Failed to load: ${escHtml(e.message)}</p>`;
    }
}

export function initHistoryView() {
    window.setHistoryDays = setHistoryDays;
    window.setHistoryRange = setHistoryRange;
    window.setHistoryMetric = setHistoryMetric;
    window.setHistoryWindow = setHistoryWindow;
    window.toggleHistoryProvider = toggleHistoryProvider;
    window.setHistoryProvidersAll = setHistoryProvidersAll;
    window.setHistoryProvidersNone = setHistoryProvidersNone;
    window.clearHistoryFilter = clearHistoryFilter;
    window.hwGoPage = hwGoPage;
}
