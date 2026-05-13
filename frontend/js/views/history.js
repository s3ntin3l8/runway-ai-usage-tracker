import { fetchHistoryDeltas } from '../api.js';
import { buildProviderSparklineStrip } from '../components.js';
import { updateCharts, destroyCharts } from '../charts.js';
import { STATE } from '../state.js';

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

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

// Short alias for use in template literals
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
    if (!historyState.activeProviders) {
        historyState.activeProviders = new Set([pid]);
    } else if (historyState.activeProviders.has(pid)) {
        historyState.activeProviders.delete(pid);
        if (historyState.activeProviders.size === 0) historyState.activeProviders = null;
    } else {
        historyState.activeProviders.add(pid);
    }
    updateCsvHref();
    loadHistoryView();
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

async function fetchHistoryWindows() {
    const params = new URLSearchParams({ days: historyState.days, page: historyState.page, limit: 50 });
    if (historyState.activeProviders?.size === 1)
        params.set('provider_id', [...historyState.activeProviders][0]);
    if (historyState.windowFilter !== 'all')
        params.set('window_type', historyState.windowFilter);
    const r = await fetch(`/api/v1/usage/history/windows?${params}`);
    if (!r.ok) throw new Error(`windows ${r.status}`);
    return r.json();
}

async function fetchHistoryChart() {
    const params = new URLSearchParams({ days: historyState.days, metric: historyState.metric });
    if (historyState.activeProviders?.size === 1)
        params.set('provider_id', [...historyState.activeProviders][0]);
    const r = await fetch(`/api/v1/usage/history/chart?${params}`);
    if (!r.ok) throw new Error(`chart ${r.status}`);
    return r.json();
}

async function fetchWindowDetail(provId, acctId, windowType, windowStart, windowEnd) {
    const params = new URLSearchParams({
        provider_id: provId,
        account_id: acctId,
        window_type: windowType,
        window_start: windowStart,
        window_end: windowEnd,
    });
    const r = await fetch(`/api/v1/usage/history/window-detail?${params}`);
    if (!r.ok) throw new Error(`window-detail ${r.status}`);
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
        <div class="t-kicker">Est. cost · period</div>
        <div class="t-val">$${totalCostDelta.toFixed(2)}<span>spent</span></div>
    </div>`;
    html += `<div class="hud-panel tile">
        <div class="t-kicker">Hottest provider</div>
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
                timestamp: bar.date + 'T12:00:00Z',
                used_value: seg.value,
                limit_value: null,
                unit_type: metric === 'cost' ? 'currency' : 'tokens',
                window_type: 'day',
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
// Window table
// ---------------------------------------------------------------------------

function renderWindowTable() {
    const container = document.getElementById('history-content');
    if (!container) return;
    const cache = historyState._windowsCache;
    if (!cache) { container.innerHTML = '<p class="ht-empty">Loading…</p>'; return; }

    const windows = cache.windows || [];
    if (!windows.length) {
        container.innerHTML = '<p class="ht-empty">No windows for selected range.</p>';
        return;
    }

    const metricHeader = historyState.metric === 'percent' ? '% USED'
        : historyState.metric === 'cost' ? 'COST' : 'TOKENS';

    const rows = windows.map((w, idx) => {
        const period = formatWindowPeriod(w);
        const fillBar = renderFillBar(w.pct_used, w.is_open);
        const mainVal = formatWindowMetric(w, historyState.metric);
        const liveBadge = w.is_open ? ' <span class="hw-live-badge">LIVE</span>' : '';

        return `<tr class="hw-row" onclick="toggleWindowExpand(this, ${idx})">
          <td class="hw-expand-btn">▶</td>
          <td class="hw-type-cell"><span class="hw-window-badge">${escHtml(w.window_type)}</span></td>
          <td class="hw-provider-cell">${escHtml(w.service_name || w.provider_id)}${liveBadge}</td>
          <td>${escHtml(w.account_label || w.account_id)}</td>
          <td>${period}</td>
          <td class="hw-metric-cell">${fillBar}${escHtml(mainVal)}</td>
        </tr>
        <tr class="hw-detail-row" id="hw-detail-${idx}" style="display:none">
          <td colspan="6"><div class="hw-detail-inner" id="hw-detail-content-${idx}">Loading…</div></td>
        </tr>`;
    }).join('');

    const metaEl = document.getElementById('history-table-meta');
    if (metaEl) metaEl.textContent = `${cache.total} windows · page ${cache.page}`;

    container.innerHTML = `
      <table class="hw-table">
        <thead><tr>
          <th></th><th>TYPE</th><th>PROVIDER</th><th>ACCOUNT</th><th>PERIOD</th><th>${metricHeader}</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${renderHWPager(cache.total, cache.page)}`;
}

function renderFillBar(pctUsed, isOpen) {
    if (pctUsed == null) return '';
    const pct = Math.min(100, Math.max(0, pctUsed));
    const color = pct >= 80 ? '#c0392b' : pct >= 50 ? '#d4a017' : '#27ae60';
    const dashStyle = isOpen ? 'stroke-dasharray="3,1"' : '';
    return `<svg class="hw-fill-bar" width="60" height="10" viewBox="0 0 60 10">
      <rect x="0" y="2" width="60" height="6" rx="2" fill="rgba(128,128,128,0.15)"/>
      <rect x="0" y="2" width="${(pct * 0.6).toFixed(1)}" height="6" rx="2" fill="${color}" ${dashStyle}/>
    </svg> `;
}

function formatWindowPeriod(w) {
    if (!w.window_start && !w.window_end) return '—';
    const fmt = iso => iso ? new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '?';
    if (w.is_open) return `${w.window_start ? fmt(w.window_start) : '…'} – now`;
    return `${fmt(w.window_start)} – ${fmt(w.window_end)}`;
}

function formatWindowMetric(w, metric) {
    if (metric === 'percent') {
        if (w.pct_used == null) return '—';
        return `${w.pct_used.toFixed(1)}%`;
    }
    if (metric === 'cost') {
        if (w.cost_usd == null) return '—';
        return `$${w.cost_usd.toFixed(2)}`;
    }
    const t = w.tokens_total;
    if (t == null) return '—';
    return t >= 1e6 ? `${(t / 1e6).toFixed(1)}M` : t >= 1e3 ? `${(t / 1e3).toFixed(0)}k` : String(t);
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
    fetchHistoryWindows().then(data => {
        historyState._windowsCache = data;
        renderWindowTable();
    }).catch(e => console.warn('page fetch failed', e));
}

// ---------------------------------------------------------------------------
// Expand detail
// ---------------------------------------------------------------------------

export async function toggleWindowExpand(row, idx) {
    const detailRow = document.getElementById(`hw-detail-${idx}`);
    const expandBtn = row.querySelector('.hw-expand-btn');
    if (!detailRow) return;

    const isOpen = detailRow.style.display !== 'none';
    if (isOpen) {
        detailRow.style.display = 'none';
        if (expandBtn) expandBtn.textContent = '▶';
        return;
    }

    detailRow.style.display = '';
    if (expandBtn) expandBtn.textContent = '▼';

    const w = (historyState._windowsCache?.windows || [])[idx];
    if (!w) return;

    const contentEl = document.getElementById(`hw-detail-content-${idx}`);
    if (!contentEl) return;
    contentEl.textContent = 'Loading…';

    try {
        const windowEnd = w.window_end || new Date().toISOString();
        // Estimate window_start from window_type duration when not available
        const durationMs = { monthly: 31, weekly: 7, daily: 1, session: 0.25 }[w.window_type] ?? 7;
        const windowStart = w.window_start
            || new Date(new Date(windowEnd).getTime() - durationMs * 86400000).toISOString();
        const detail = await fetchWindowDetail(
            w.provider_id, w.account_id, w.window_type, windowStart, windowEnd
        );
        const html = renderWindowDetailHTML(detail);
        if (!html) {
            detailRow.style.display = 'none';
            if (expandBtn) expandBtn.textContent = '▶';
        } else {
            contentEl.innerHTML = html;
        }
    } catch (e) {
        contentEl.textContent = `Failed to load detail: ${e.message}`;
    }
}

function renderWindowDetailHTML(detail) {
    const hasFill = (detail.fill_series || []).length > 0;
    const hasModels = (detail.by_model || []).length > 0;
    if (!hasFill && !hasModels) return null;  // caller will suppress the expand row

    // Deduplicate fill_series: keep last snapshot per calendar day
    const dayMap = new Map();
    for (const p of (detail.fill_series || [])) {
        const day = p.ts.slice(0, 10); // YYYY-MM-DD
        dayMap.set(day, p);
    }
    const fillRows = [...dayMap.values()].map(p => {
        const date = new Date(p.ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        return `<tr><td>${date}</td><td>${p.pct_used != null ? p.pct_used.toFixed(1) + '%' : '—'}</td></tr>`;
    }).join('');

    const totalTokens = (detail.by_model || []).reduce((s, m) => s + (m.tokens || 0), 0);
    const modelRows = (detail.by_model || []).map(m => {
        const share = totalTokens ? ((m.tokens / totalTokens) * 100).toFixed(0) + '%' : '—';
        const tok = m.tokens >= 1e6
            ? `${(m.tokens / 1e6).toFixed(1)}M`
            : m.tokens >= 1e3 ? `${(m.tokens / 1e3).toFixed(0)}k` : String(m.tokens || 0);
        return `<tr>
          <td>${escHtml(m.model_id || '—')}</td>
          <td>${share}</td>
          <td>${tok}</td>
          <td>$${(m.cost_usd || 0).toFixed(2)}</td>
          <td>${m.msgs || 0}</td>
        </tr>`;
    }).join('');

    const fillSection = fillRows ? `
      <div>
        <p class="hw-detail-label">HOW IT FILLED UP</p>
        <table class="hw-detail-table">
          <thead><tr><th>DATE</th><th>% USED</th></tr></thead>
          <tbody>${fillRows}</tbody>
        </table>
      </div>` : '';

    const modelSection = modelRows ? `
      <div>
        <p class="hw-detail-label">BY MODEL</p>
        <table class="hw-detail-table">
          <thead><tr><th>MODEL</th><th>SHARE</th><th>TOKENS</th><th>COST</th><th>MSGS</th></tr></thead>
          <tbody>${modelRows}</tbody>
        </table>
      </div>` : '';

    return `<div class="hw-detail-panels">${fillSection}${modelSection}</div>`;
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
    renderWindowTable();
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
        const [windows, chart, deltas] = await Promise.all([
            fetchHistoryWindows(),
            fetchHistoryChart(),
            fetchHistoryDeltas({ days: historyState.days }).catch(e => {
                console.warn('deltas fetch failed', e);
                return null;
            }),
        ]);
        historyState._windowsCache = windows;
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
    window.toggleWindowExpand = toggleWindowExpand;
    window.hwGoPage = hwGoPage;
}
