/**
 * Usage pane builder for the provider detail modal.
 *
 * Data sources:
 *   - heatmapData : fetchHeatmap() response ({ cells: [...168 values...] })
 *   - sessions    : fetchSessions() response ({ sessions: [...] })
 *   - throughputCells : same cells array reused for throughput sparkline
 */

import { formatLocalTime, getUserTz } from '../../utils/tz.js';
import { escapeHTML as _esc } from '../../utils/html.js';
import { formatTokens as _fmtTokens, formatCost as _fmtCost } from '../../utils/format.js';
import { niceMax as _niceMax, fmtTick as _fmtTick } from '../../utils/chart-scale.js';

// Module-level state for the sparkline hover — updated on each tab switch.
let _sparkSeries = [];
let _sparkRange  = '24h';

function _fmtDuration(sec) {
    if (!sec || sec < 60) return '<1 min';
    if (sec < 3600) return `${Math.round(sec / 60)} min`;
    const h = Math.floor(sec / 3600);
    const m = Math.round((sec % 3600) / 60);
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

/**
 * Build a throughput sparkline SVG from heatmap cells.
 * @returns {{ svgHtml: string, yTicks: Array<{y: string, label: string}>, series: number[] }}
 */
function _buildSparkSvg(cells, range) {
    if (!cells || !cells.length) {
        return {
            svgHtml: '<svg id="pu-spark-svg" viewBox="0 0 720 140" preserveAspectRatio="none"><text x="360" y="75" text-anchor="middle" font-size="10" fill="var(--ink-3)" font-family="var(--mono)">No data</text></svg>',
            yTicks: [],
            series: [],
        };
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

    const rawMax  = Math.max(...series, 1);
    const niceMax = _niceMax(rawMax);
    const w = 720, h = 140;
    const xStep = w / (series.length - 1 || 1);

    // Map a data value to an SVG y coordinate (6px top margin, 6px bottom margin)
    const toY = v => h - (v / niceMax) * (h - 12) - 6;

    const points = series.map((v, i) => ({ x: i * xStep, y: toY(v) }));
    const linePts = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ');
    const fillPts = `M 0 ${h} ${points.map(p => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')} L${w} ${h} Z`;

    // Three grid lines at 25%, 50%, 75% of niceMax; labels use _fmtTick for precision
    const yTicks = [0.75, 0.50, 0.25].map(frac => ({
        y:     toY(niceMax * frac).toFixed(1),
        label: _fmtTick(niceMax * frac),
    }));

    const svgHtml = `<svg id="pu-spark-svg" viewBox="0 0 720 140" preserveAspectRatio="none">
        <defs>
            <linearGradient id="pu-sg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.30"/>
                <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
            </linearGradient>
        </defs>
        ${yTicks.map(t => `<line x1="0" y1="${t.y}" x2="720" y2="${t.y}" stroke="var(--hairline-2)" stroke-dasharray="2 4"/>`).join('\n        ')}
        <path d="${fillPts}" fill="url(#pu-sg)"/>
        <path d="${linePts}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>
    </svg>`;

    return { svgHtml, yTicks, series };
}

/** Build X-axis label strip HTML for the given time range. */
function _buildXLabels(range) {
    // 7d labels are Sun-first to match the underlying heatmap cell order
    // (day_of_week=0 is Sun). The heatmap grid applies its own Mon-first reorder;
    // the sparkline does not — so Sun-first labels are correct for this chart.
    const labels = range === '24h'
        ? ['00:00', '06:00', '12:00', '18:00', '24:00']
        : range === '7d'
            ? ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
            : ['', '+7d', '+14d', '+21d', '+28d'];
    return `<div class="m-spark-x-axis" id="pu-x-axis">${
        labels.map(l => `<span>${_esc(l)}</span>`).join('')
    }</div>`;
}

/** Build heatmap cells HTML — 7 × 24 grid, Mon-first row order. */
function _buildHeatGrid(rawCells, accentHue) {
    // rawCells: flat 168 values where index = day_of_week(0=Sun)*24 + hour
    // Each element may be a plain number or a {dow, hour, tokens, cost_usd} dict.
    // Normalise to {tokens, cost} pairs.
    const flatRaw = rawCells.map(c =>
        c != null && typeof c === 'object'
            ? { tokens: c.tokens || 0, cost: c.cost_usd || 0 }
            : { tokens: c || 0, cost: 0 }
    );
    // Reorder so Mon first: days [Mon=1, Tue=2, Wed=3, Thu=4, Fri=5, Sat=6, Sun=0]
    const reorder = [1, 2, 3, 4, 5, 6, 0];
    const cellsTokens = [];
    const cellsCost = [];
    for (let r = 0; r < 7; r++) {
        const d = reorder[r];
        for (let h = 0; h < 24; h++) {
            const cell = flatRaw[d * 24 + h] || { tokens: 0, cost: 0 };
            cellsTokens.push(cell.tokens || 0);
            cellsCost.push(cell.cost || 0);
        }
    }

    // Log-normalize on tokens (the metric that drives cell color).
    const maxVal = Math.max(...cellsTokens, 1);
    const norm = cellsTokens.map(v => v > 0 ? Math.log1p(v) / Math.log1p(maxVal) : 0);

    // Find peak cell
    const dayLabels = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
    const peakIdx = norm.reduce((m, v, i) => v > norm[m] ? i : m, 0);
    const peakDay = dayLabels[Math.floor(peakIdx / 24)];
    const peakHr  = peakIdx % 24;
    const peakStr = `${peakDay.charAt(0)}${peakDay.slice(1).toLowerCase()} ${String(peakHr).padStart(2, '0')}:00`;

    const totalTokens = cellsTokens.reduce((s, v) => s + v, 0);

    const hue = accentHue || 40;  // fallback to amber-ish
    const gridHtml = norm.map((v, idx) => {
        const day = dayLabels[Math.floor(idx / 24)];
        const hour = idx % 24;
        return `<i style="background: oklch(${(0.95 - v * 0.55).toFixed(3)} ${(0.02 + v * 0.16).toFixed(3)} ${hue})" data-dow-label="${day}" data-hour="${hour}" data-tokens="${cellsTokens[idx]}" data-cost="${cellsCost[idx]}"></i>`;
    }).join('');

    return { gridHtml, peakStr, totalTokens };
}

/** Build HTML for a single session card. Exported for use by other panes. */
export function buildSessionCard(s) {
    const start = formatLocalTime(s.ts_start);
    const end   = s.ts_end ? formatLocalTime(s.ts_end) : '…';
    const time  = `${start}–${end}`;

    const byModel  = Array.isArray(s.by_model) ? s.by_model : [];
    const nModels  = byModel.length || (s.models?.length ?? 0);
    const modelLabel = nModels === 1
        ? (byModel[0]?.model_id || s.models?.[0] || 'unknown')
        : nModels > 1
            ? `${nModels} models`
            : 'unknown';
    const dur   = _fmtDuration(s.duration_seconds || 0);
    const subN  = s.subagent_msgs || 0;
    const turns = s.msgs
        ? (subN > 0 ? `${s.msgs} turns (${subN} via subagent)` : `${s.msgs} turns`)
        : '';
    const desc  = [modelLabel, dur, turns].filter(Boolean).join(' · ');

    const tok  = s.tokens_total ? _fmtTokens(s.tokens_total) + ' tok' : '';
    const cost = s.cost_usd ? _fmtCost(s.cost_usd) : '';
    const val  = [tok, cost].filter(Boolean).join(' · ') || '—';

    const tok_in    = s.tokens_input        ? _fmtTokens(s.tokens_input)        + ' in'    : '';
    const tok_out   = s.tokens_output       ? _fmtTokens(s.tokens_output)       + ' out'   : '';
    const tok_read  = s.tokens_cache_read   ? _fmtTokens(s.tokens_cache_read)   + ' read'  : '';
    const tok_write = s.tokens_cache_create ? _fmtTokens(s.tokens_cache_create) + ' write' : '';
    const cch_pct   = s.cache_pct > 0 ? `cache ${s.cache_pct}%` : '';
    const detail    = [tok_in, tok_out, tok_read, tok_write, cch_pct].filter(Boolean).join(' · ');

    const modelRows = nModels > 1
        ? byModel.map(m => {
            const tot   = `${_fmtTokens(m.tokens_total)} tok`;
            const io    = (m.tokens_input || m.tokens_output)
                ? `${_fmtTokens(m.tokens_input)} in / ${_fmtTokens(m.tokens_output)} out`
                : '';
            const cache = (m.tokens_cache_read || m.tokens_cache_create)
                ? `${_fmtTokens((m.tokens_cache_read || 0) + (m.tokens_cache_create || 0))} cache`
                : '';
            const breakdown = [io, cache].filter(Boolean).join(' · ');
            const tokPart   = breakdown ? `${tot} (${breakdown})` : tot;
            const costStr   = m.cost_usd ? _fmtCost(m.cost_usd) : '';
            const parts     = [`${m.model_id} × ${m.msgs} turns`, tokPart, costStr].filter(Boolean);
            return `<span class="m-model-row">${_esc(`⊢ ${parts.join(' · ')}`)}</span>`;
        }).join('')
        : '';

    const subagents = Array.isArray(s.subagents) ? s.subagents : [];
    const subagentRows = subagents.map(a =>
        `<span class="m-subagents">${_esc(`↳ ${a.type} × ${a.turns} · ${_fmtTokens(a.tokens_total)} tok · ${_fmtCost(a.cost_usd)}`)}</span>`
    ).join('');

    const modelsSep = modelRows
        ? '<span class="m-agents-sep">models</span>'
        : '';

    const agentsSep = modelRows && subagentRows
        ? '<span class="m-agents-sep">agents</span>'
        : '';

    const cls = (s.tokens_total || 0) > 500000 ? 'warn' : 'good';
    return `<div class="m-event ${cls}">
        <span class="t">${_esc(time)}</span>
        <span class="dot"></span>
        <span class="msg">${_esc(desc)}</span>
        <span class="v">${_esc(val)}</span>
        ${detail ? `<span class="m-detail">${_esc(detail)}</span>` : ''}
        ${modelsSep}
        ${modelRows}
        ${agentsSep}
        ${subagentRows}
    </div>`;
}

/** Build top sessions HTML from sessions array. */
function _buildSessionsHtml(sessions) {
    if (!sessions || !sessions.length) {
        return '<div class="m-event"><span class="t">—</span><span class="dot"></span><span class="msg">No sessions yet</span><span class="v"></span></div>';
    }
    return sessions.slice(0, 8).map(buildSessionCard).join('');
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
    const { gridHtml, peakStr, totalTokens } = _buildHeatGrid(rawCells, accentHue);
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const { svgHtml: sparkHtml, yTicks, series } = _buildSparkSvg(rawCells, '24h');
    _sparkSeries = series;
    _sparkRange  = '24h';

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
            <div class="m-heat-chart-wrap">
                <div class="m-heat" data-total-tokens="${totalTokens}">
                    ${gridHtml}
                </div>
                <div class="m-chart-tip" hidden></div>
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
            <h4>Throughput <span style="font-weight:500;font-size:9px;letter-spacing:.04em;color:var(--ink-3);margin-left:4px" id="pu-spark-unit">avg tok/hr</span></h4>
            <span class="m-spark-tabs" id="pu-spark-tabs">
                <button data-range="24h" class="on">24h</button>
                <button data-range="7d">7d</button>
                <button data-range="30d">30d</button>
            </span>
        </div>
        <div class="m-spark-body">
            <div class="m-spark-y-axis" id="pu-y-axis">${
                yTicks.map(t => `<span style="top:${t.y}px">${_esc(t.label)}</span>`).join('')
            }</div>
            <div class="m-spark-chart-area" id="pu-chart-area">
                ${sparkHtml}
                <div class="m-spark-tip" id="pu-spark-tip" hidden></div>
            </div>
        </div>
        ${_buildXLabels('24h')}
    </div>
    `;
}

/**
 * Wire spark tab click → rebuild throughput sparkline, y-axis labels, x-axis, and hover state.
 * Call after injecting usage pane HTML into the DOM.
 */
export function wireUsageSparkTabs(heatmapCells) {
    const tabs = document.getElementById('pu-spark-tabs');
    if (!tabs) return;
    tabs.addEventListener('click', e => {
        const btn = e.target.closest('button');
        if (!btn) return;
        tabs.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));

        const range = btn.dataset.range;
        const { svgHtml, yTicks, series } = _buildSparkSvg(heatmapCells, range);

        // Update module-level hover state
        _sparkSeries = series;
        _sparkRange  = range;

        // Swap SVG
        const area = document.getElementById('pu-chart-area');
        if (area) {
            const oldSvg = area.querySelector('svg');
            if (oldSvg) {
                const tmp = document.createElement('div');
                tmp.innerHTML = svgHtml;
                const svgEl = tmp.querySelector('svg');
                if (svgEl) oldSvg.replaceWith(svgEl);
            }
        }

        // Rebuild Y-axis labels
        const yAxis = document.getElementById('pu-y-axis');
        if (yAxis) {
            yAxis.innerHTML = yTicks.map(t => `<span style="top:${t.y}px">${_esc(t.label)}</span>`).join('');
        }

        // Rebuild X-axis labels
        const xOld = document.getElementById('pu-x-axis');
        if (xOld) {
            const tmp2 = document.createElement('div');
            tmp2.innerHTML = _buildXLabels(range);
            const xNew = tmp2.firstElementChild;
            if (xNew) xOld.replaceWith(xNew);
        }

        // Update unit label
        const unitEl = document.getElementById('pu-spark-unit');
        if (unitEl) {
            unitEl.textContent = range === '24h' ? 'avg tok/hr' : range === '7d' ? 'tok/hr by day' : 'avg tok/day';
        }
    });
}

/**
 * Wire hover-tooltip to the throughput sparkline.
 * Reads from module-level _sparkSeries / _sparkRange (updated by wireUsageSparkTabs).
 * Call once after injecting usage pane HTML into the DOM.
 */
export function wireUsageSparkHover() {
    const area = document.getElementById('pu-chart-area');
    const tip  = document.getElementById('pu-spark-tip');
    if (!area || !tip) return;

    const getLabel = (i, range) => {
        if (range === '24h') return `${String(i).padStart(2, '0')}:00`;
        if (range === '7d') {
            const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            return `${DAYS[Math.floor(i / 24)]} ${String(i % 24).padStart(2, '0')}:00`;
        }
        return `Day ${i + 1}`;
    };

    area.addEventListener('mousemove', e => {
        if (!_sparkSeries.length) return;
        const rect = area.getBoundingClientRect();
        const xFrac = (e.clientX - rect.left) / rect.width;
        const idx = Math.max(0, Math.min(_sparkSeries.length - 1, Math.round(xFrac * (_sparkSeries.length - 1))));
        const val = _sparkSeries[idx];
        const label = getLabel(idx, _sparkRange);
        tip.innerHTML = `<span style="color:var(--ink-3)">${_esc(label)}</span>&nbsp;&nbsp;<b style="color:var(--accent)">${_esc(_fmtTick(val))}</b> tok`;
        tip.hidden = false;
        // Keep tooltip inside chart area
        const tipW = tip.offsetWidth || 130;
        let tx = e.clientX - rect.left + 12;
        if (tx + tipW > rect.width - 4) tx = e.clientX - rect.left - tipW - 12;
        tip.style.transform = `translate(${Math.max(0, tx)}px, 0)`;
    });

    area.addEventListener('mouseleave', () => { tip.hidden = true; });
}

/**
 * Wire hover-tooltip behavior to the hour-of-day heatmap.
 * Call after injecting usage pane HTML into the DOM.
 */
export function wireUsageHeatmapTooltip() {
    const body = document.getElementById('pm-body');
    if (!body) return;
    const wrap = body.querySelector('.m-heat-chart-wrap');
    const grid = body.querySelector('.m-heat');
    const tip  = body.querySelector('.m-chart-tip');
    if (!wrap || !grid || !tip) return;

    const totalTokens = parseFloat(grid.getAttribute('data-total-tokens') || '0') || 0;

    const render = (cell) => {
        const day    = cell.getAttribute('data-dow-label') || '';
        const hour   = parseInt(cell.getAttribute('data-hour') || '0', 10);
        const tokens = parseFloat(cell.getAttribute('data-tokens') || '0') || 0;
        const cost   = parseFloat(cell.getAttribute('data-cost') || '0') || 0;
        const share  = totalTokens > 0
            ? (tokens / totalTokens * 100).toFixed(1) + '%'
            : '—';
        const hourStr = String(hour).padStart(2, '0') + ':00';

        tip.innerHTML = `
            <div class="m-chart-tip-head">${_esc(day)} · ${_esc(hourStr)}</div>
            <div class="m-chart-tip-row"><span class="sw"></span><span class="lbl">Tokens</span><span class="val">${_esc(_fmtTokens(tokens))}</span></div>
            <div class="m-chart-tip-row"><span class="sw"></span><span class="lbl">Cost</span><span class="val">${_esc(_fmtCost(cost))}</span></div>
            <div class="m-chart-tip-row"><span class="sw sw-dim"></span><span class="lbl">Share</span><span class="val">${_esc(share)}</span></div>
        `;
        tip.hidden = false;
    };

    const position = (e) => {
        const wrapRect = wrap.getBoundingClientRect();
        const tipW = tip.offsetWidth || 180;
        const tipH = tip.offsetHeight || 80;
        let tx = e.clientX - wrapRect.left + 12;
        if (tx + tipW > wrapRect.width - 4) tx = e.clientX - wrapRect.left - tipW - 12;
        if (tx < 4) tx = 4;
        let ty = e.clientY - wrapRect.top + 12;
        if (ty + tipH > wrapRect.height - 4) ty = e.clientY - wrapRect.top - tipH - 12;
        if (ty < 4) ty = 4;
        tip.style.transform = `translate(${tx}px, ${ty}px)`;
    };

    grid.addEventListener('mousemove', (e) => {
        const cell = e.target.closest('i[data-hour]');
        if (!cell || !grid.contains(cell)) return;
        render(cell);
        position(e);
    });

    grid.addEventListener('mouseleave', () => { tip.hidden = true; });
}
