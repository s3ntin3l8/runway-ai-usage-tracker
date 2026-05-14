/**
 * Usage pane builder for the provider detail modal.
 *
 * Data sources:
 *   - heatmapData : fetchHeatmap() response ({ cells: [...168 values...] })
 *   - sessions    : fetchSessions() response ({ sessions: [...] })
 *   - throughputCells : same cells array reused for throughput sparkline
 */

import { formatLocalTime, getUserTz } from '../../utils/tz.js';

function _esc(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function _fmtTokens(val) {
    if (val == null || val === 0) return '0';
    if (val >= 1e9) return (val / 1e9).toFixed(2) + 'B';
    if (val >= 1e6) return (val / 1e6).toFixed(2) + 'M';
    if (val >= 1e3) return (val / 1e3).toFixed(0) + 'K';
    return String(val);
}

function _fmtCost(usd) {
    if (usd == null) return '—';
    if (usd === 0) return '$0.00';
    if (usd < 0.01) return '<$0.01';
    return '$' + usd.toFixed(2);
}

function _fmtDuration(sec) {
    if (!sec || sec < 60) return '<1 min';
    if (sec < 3600) return `${Math.round(sec / 60)} min`;
    const h = Math.floor(sec / 3600);
    const m = Math.round((sec % 3600) / 60);
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

/** Build a throughput sparkline SVG from heatmap cells. */
function _buildSparkSvg(cells, range) {
    if (!cells || !cells.length) {
        return '<svg id="pu-spark-svg" viewBox="0 0 720 140" preserveAspectRatio="none"><text x="360" y="75" text-anchor="middle" font-size="10" fill="var(--ink-3)" font-family="var(--mono)">No data</text></svg>';
    }
    // Normalise cells: accept either raw numbers or {tokens:n} dicts from the heatmap API
    const flatCells = cells.map(c => (c != null && typeof c === 'object' ? (c.tokens || 0) : (c || 0)));
    const n = range === '24h' ? 24 : range === '7d' ? 7 * 24 : 30;
    const step = Math.max(1, Math.floor(flatCells.length / n));
    const series = [];
    for (let i = 0; i < n; i++) {
        const slice = flatCells.slice(i * step, (i + 1) * step);
        series.push(slice.reduce((a, v) => a + v, 0) / (slice.length || 1));
    }
    const maxVal = Math.max(...series, 1);
    const w = 720, h = 140;
    const xStep = w / (series.length - 1 || 1);
    const points = series.map((v, i) => ({ x: i * xStep, y: h - (v / maxVal) * (h - 12) - 6 }));
    const linePts = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ');
    const fillPts = `M 0 ${h} ${points.map(p => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')} L${w} ${h} Z`;
    return `<svg id="pu-spark-svg" viewBox="0 0 720 140" preserveAspectRatio="none">
        <defs>
            <linearGradient id="pu-sg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.30"/>
                <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
            </linearGradient>
        </defs>
        <line x1="0" y1="35"  x2="720" y2="35"  stroke="var(--hairline-2)" stroke-dasharray="2 4"/>
        <line x1="0" y1="70"  x2="720" y2="70"  stroke="var(--hairline-2)" stroke-dasharray="2 4"/>
        <line x1="0" y1="105" x2="720" y2="105" stroke="var(--hairline-2)" stroke-dasharray="2 4"/>
        <path d="${fillPts}" fill="url(#pu-sg)"/>
        <path d="${linePts}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>
    </svg>`;
}

/** Build heatmap cells HTML — 7 × 24 grid, Mon-first row order. */
function _buildHeatGrid(rawCells, accentHue) {
    // rawCells: flat 168 values where index = day_of_week(0=Sun)*24 + hour
    // Each element may be a plain number or a {dow, hour, tokens} dict from the API.
    // Normalise to plain numbers first.
    const flatRaw = rawCells.map(c => (c != null && typeof c === 'object' ? (c.tokens || 0) : (c || 0)));
    // Reorder so Mon first: days [Mon=1, Tue=2, Wed=3, Thu=4, Fri=5, Sat=6, Sun=0]
    const reorder = [1, 2, 3, 4, 5, 6, 0];
    const cells = [];
    for (let r = 0; r < 7; r++) {
        const d = reorder[r];
        for (let h = 0; h < 24; h++) {
            cells.push(flatRaw[d * 24 + h] || 0);
        }
    }

    // Log-normalize
    const maxVal = Math.max(...cells, 1);
    const norm = cells.map(v => v > 0 ? Math.log1p(v) / Math.log1p(maxVal) : 0);

    // Find peak cell
    const peakIdx = norm.reduce((m, v, i) => v > norm[m] ? i : m, 0);
    const peakDay = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][Math.floor(peakIdx / 24)];
    const peakHr  = peakIdx % 24;
    const peakStr = `${peakDay} ${String(peakHr).padStart(2, '0')}:00`;

    const hue = accentHue || 40;  // fallback to amber-ish
    const gridHtml = norm.map(v =>
        `<i style="background: oklch(${(0.95 - v * 0.55).toFixed(3)} ${(0.02 + v * 0.16).toFixed(3)} ${hue})"></i>`
    ).join('');

    return { gridHtml, peakStr };
}

/** Build top sessions HTML from sessions array. */
function _buildSessionsHtml(sessions) {
    if (!sessions || !sessions.length) {
        return '<div class="m-event"><span class="t">—</span><span class="dot"></span><span class="msg">No sessions yet</span><span class="v"></span></div>';
    }
    return sessions.slice(0, 8).map(s => {
        const start = formatLocalTime(s.ts_start);
        const end   = s.ts_end ? formatLocalTime(s.ts_end) : '…';
        const time  = `${start}–${end}`;

        const modelLabel = s.models && s.models.length === 1
            ? s.models[0]
            : s.models && s.models.length > 1
                ? `${s.models.length} models`
                : 'unknown';
        const dur   = _fmtDuration(s.duration_seconds || 0);
        const turns = s.msgs ? `${s.msgs} turns` : '';
        const desc  = [modelLabel, dur, turns].filter(Boolean).join(' · ');

        const tok  = s.tokens_total ? _fmtTokens(s.tokens_total) + ' tok' : '';
        const cost = s.cost_usd ? _fmtCost(s.cost_usd) : '';
        const val  = [tok, cost].filter(Boolean).join(' · ') || '—';

        const tok_in  = s.tokens_input  ? _fmtTokens(s.tokens_input)  + ' in'    : '';
        const tok_out = s.tokens_output ? _fmtTokens(s.tokens_output) + ' out'   : '';
        const tok_cch = s.tokens_cache  ? _fmtTokens(s.tokens_cache)  + ' cache' : '';
        const hit_pct = s.cache_hit_pct > 0 ? `hit ${s.cache_hit_pct}%` : '';
        const detail  = [tok_in, tok_out, tok_cch, hit_pct].filter(Boolean).join(' · ');

        const cls = (s.tokens_total || 0) > 500000 ? 'warn' : 'good';
        return `<div class="m-event ${cls}">
            <span class="t">${_esc(time)}</span>
            <span class="dot"></span>
            <span class="msg">${_esc(desc)}</span>
            <span class="v">${_esc(val)}</span>
            ${detail ? `<span class="m-detail">${_esc(detail)}</span>` : ''}
        </div>`;
    }).join('');
}

/**
 * Build the Usage pane HTML string.
 *
 * @param {object} entry - Fleet entry from STATE.fleet
 * @param {object|null} heatmapData - Response from fetchHeatmap()
 * @param {Array|null} sessions - Array of session objects from fetchSessions()
 * @returns {string} HTML string
 */
export function buildUsagePane(entry, heatmapData, sessions) {
    const rawCells = heatmapData?.cells || new Array(168).fill(0);
    const accentHue = 40; // amber default; could read from CSS variable but not critical
    const { gridHtml, peakStr } = _buildHeatGrid(rawCells, accentHue);
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const sparkHtml = _buildSparkSvg(rawCells, '24h');

    const tzLabel = getUserTz();

    return `
    <!-- HEATMAP -->
    <div class="m-block">
        <div class="head">
            <h4>Hour-of-day pattern · last 14 days</h4>
            <span class="meta">peak <b style="color:var(--ink)">${_esc(peakStr)}</b> · times in ${_esc(tzLabel)}</span>
        </div>
        <div class="m-heat-wrap">
            <div class="m-heat-axis">${days.map(d => `<span>${_esc(d)}</span>`).join('')}</div>
            <div class="m-heat">
                ${gridHtml}
            </div>
        </div>
        <div class="m-heat-foot">
            <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>23:00</span>
        </div>
        <div class="m-heat-foot" style="margin-top:10px">
            <span>peak: <b style="color:var(--ink)">${_esc(peakStr)}</b></span>
            <span class="m-heat-legend">
                low
                <i style="background: oklch(0.92 0.04 ${accentHue})"></i>
                <i style="background: oklch(0.78 0.10 ${accentHue})"></i>
                <i style="background: oklch(0.62 0.16 ${accentHue})"></i>
                <i style="background: oklch(0.45 0.22 ${accentHue})"></i>
                high
            </span>
        </div>
    </div>

    <!-- TOP SESSIONS -->
    <div class="m-block">
        <div class="head">
            <h4>Top sessions · this window</h4>
            <span class="meta">attributed by sidecar</span>
        </div>
        <div class="m-events">
            ${_buildSessionsHtml(sessions)}
        </div>
    </div>

    <!-- THROUGHPUT SPARKLINE -->
    <div class="m-spark" id="pu-spark-wrap">
        <div class="m-spark-head">
            <h4>Throughput</h4>
            <span class="m-spark-tabs" id="pu-spark-tabs">
                <button data-range="24h" class="on">24h</button>
                <button data-range="7d">7d</button>
                <button data-range="30d">30d</button>
            </span>
        </div>
        ${sparkHtml}
    </div>
    `;
}

/**
 * Wire spark tab click → rebuild throughput sparkline.
 * Call after injecting usage pane HTML into the DOM.
 */
export function wireUsageSparkTabs(heatmapCells) {
    const tabs = document.getElementById('pu-spark-tabs');
    if (!tabs) return;
    tabs.addEventListener('click', e => {
        const btn = e.target.closest('button');
        if (!btn) return;
        tabs.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
        const wrap = document.getElementById('pu-spark-wrap');
        if (!wrap) return;
        const oldSvg = wrap.querySelector('svg');
        if (oldSvg) {
            const tmp = document.createElement('div');
            tmp.innerHTML = _buildSparkSvg(heatmapCells, btn.dataset.range);
            const svgEl = tmp.querySelector('svg');
            if (svgEl) oldSvg.replaceWith(svgEl);
        }
    });
}
