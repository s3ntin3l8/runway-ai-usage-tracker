/**
 * Overview pane builder for the provider detail modal.
 *
 * Data sources:
 *   - entry   : fleet entry (critical_gauge, secondary_limits, sidecar_contributions)
 *   - cumData : fetchCumulative() row for this account, from STATE.cumulativeMap
 *   - heatmap : fetchHeatmap() response (for sparkline rendering)
 *   - events  : fetchEvents() response (recent events log)
 */

import { providerDisplayLabel } from '../../components.js';

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

function _fmtAgo(isoStr) {
    if (!isoStr) return '—';
    const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
    if (diff < 60) return Math.round(diff) + 's';
    if (diff < 3600) return Math.round(diff / 60) + 'm';
    if (diff < 86400) return Math.round(diff / 3600) + 'h';
    return Math.round(diff / 86400) + 'd';
}

function _osGlyph(os) {
    if (!os) return '◈ ';
    const l = os.toLowerCase();
    if (l.includes('darwin') || l.includes('mac')) return '⌘ ';
    if (l.includes('win')) return '⊞ ';
    if (l.includes('linux')) return '🐧 ';
    return '◈ ';
}

/** Build the donut SVG for model mix (matches v4 `donutSvg`). */
function _donutSvg(slices) {
    if (!slices || !slices.length) {
        return `<svg class="donut" viewBox="0 0 120 120">
            <circle cx="60" cy="60" r="44" fill="none" stroke="var(--hairline-2)" stroke-width="20"/>
            <text x="60" y="62" text-anchor="middle" dominant-baseline="middle" class="center-pct">—</text>
        </svg>`;
    }
    const total = slices.reduce((a, s) => a + (s.share || 0), 0) || 1;
    const r = 44, cx = 60, cy = 60, circumference = 2 * Math.PI * r;
    let offset = 0;
    const segs = slices.map(s => {
        const pct = (s.share || 0) / total;
        const dash = pct * circumference;
        const gap = circumference - dash;
        const seg = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none"
            stroke="oklch(0.62 0.16 ${s.color || 60})"
            stroke-width="20"
            stroke-dasharray="${dash.toFixed(2)} ${gap.toFixed(2)}"
            stroke-dashoffset="${(-offset * circumference / total).toFixed(2)}"
            transform="rotate(-90 ${cx} ${cy})"/>`;
        offset += (s.share || 0);
        return seg;
    });
    const topShare = Math.round(slices[0]?.share || 0);
    return `<svg class="donut" viewBox="0 0 120 120">
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--surface-3)" stroke-width="20"/>
        ${segs.join('')}
        <text x="${cx}" y="${cy - 6}" text-anchor="middle" class="center-pct">${topShare}%</text>
        <text x="${cx}" y="${cy + 10}" text-anchor="middle" class="center-lab">MODELS</text>
    </svg>`;
}

/**
 * Build the model mix slices from cumData (by_model in current month bucket)
 * or fall back to by_model on fleet entry cards.
 */
function _buildModelMix(entry, cumData) {
    // Prefer the cumulative month bucket's by_model for accurate period stats
    const monthBucket = _cumBucket(cumData, 'month');
    const sourceByModel = (monthBucket && Object.keys(monthBucket.by_model || {}).length)
        ? monthBucket.by_model
        : null;

    const byModel = {};
    if (sourceByModel) {
        for (const [mdl, stats] of Object.entries(sourceByModel)) {
            const tok = (stats.tokens_input || 0) + (stats.tokens_output || 0) +
                        (stats.tokens_cache_read || 0) + (stats.tokens_reasoning || 0);
            byModel[mdl] = { tok, cost: stats.cost_usd || 0, msgs: stats.msgs || 0 };
        }
    } else {
        // Fall back to by_model on the fleet entry cards
        const allCards = [entry.critical_gauge, ...(entry.secondary_limits || [])].filter(Boolean);
        for (const c of allCards) {
            if (!c.by_model) continue;
            for (const [mdl, stats] of Object.entries(c.by_model)) {
                if (!byModel[mdl]) byModel[mdl] = { tok: 0, cost: 0, msgs: 0 };
                byModel[mdl].tok  += (stats.tokens_total || stats.tokens || 0);
                byModel[mdl].cost += (stats.cost_usd || 0);
                byModel[mdl].msgs += (stats.msgs || 0);
            }
        }
    }

    const entries = Object.entries(byModel).sort((a, b) => b[1].tok - a[1].tok);
    const total = entries.reduce((s, [, v]) => s + v.tok, 0) || 1;
    const HUE_START = 28;
    return entries.map(([name, v], i) => ({
        name,
        tok: _fmtTokens(v.tok),
        share: Math.round((v.tok / total) * 100),
        color: (HUE_START + i * 80) % 360,
    }));
}

/**
 * Build sidecar rows from entry.sidecar_contributions.
 * Returns array of { id, name, os, delta, deltaRaw, cost, costRaw, status, ago, spark }.
 */
function _buildSidecarRows(entry) {
    const contributions = entry.sidecar_contributions || {};
    const rows = [];
    for (const [sid, stats] of Object.entries(contributions)) {
        const totalTok = (stats.tokens_input || 0) + (stats.tokens_output || 0);
        rows.push({
            id: sid,
            name: sid,
            os: stats.os || '',
            deltaRaw: totalTok,
            delta: '+' + _fmtTokens(totalTok),
            costRaw: stats.cost_usd || 0,
            cost: _fmtCost(stats.cost_usd || 0),
            status: 'good',
            ago: stats.last_seen ? _fmtAgo(stats.last_seen) : '—',
        });
    }
    rows.sort((a, b) => b.deltaRaw - a.deltaRaw);
    return rows;
}

/** Assign stable hues to sidecar IDs. */
function _scHue(id, idx) {
    // Hash the ID for a stable hue across renders
    let hash = 0;
    for (const ch of String(id)) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
    return Math.abs(hash) % 360 || (idx * 47 + 60) % 360;
}

/**
 * Build the sparkline SVG from heatmap data.
 * Aggregates cell values into a series of the requested length.
 */
function _buildSparkSvg(cells, range) {
    if (!cells || !cells.length) {
        return '<svg id="pm-spark-svg" viewBox="0 0 720 140" preserveAspectRatio="none"><text x="360" y="75" text-anchor="middle" font-size="10" fill="var(--ink-3)" font-family="var(--mono)">No data</text></svg>';
    }
    // Aggregate cells into n buckets
    const n = range === '24h' ? 24 : range === '7d' ? 7 * 24 : 30;
    const step = Math.max(1, Math.floor(cells.length / n));
    const series = [];
    for (let i = 0; i < n; i++) {
        const slice = cells.slice(i * step, (i + 1) * step);
        series.push(slice.reduce((a, v) => a + (v || 0), 0) / (slice.length || 1));
    }
    const maxVal = Math.max(...series, 1);
    const w = 720, h = 140;
    const xStep = w / (series.length - 1 || 1);
    const points = series.map((v, i) => {
        const x = i * xStep;
        const y = h - (v / maxVal) * (h - 12) - 6;
        return { x, y };
    });
    const linePts = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ');
    const fillPts = `M 0 ${h} ${points.map(p => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')} L${w} ${h} Z`;
    return `<svg id="pm-spark-svg" viewBox="0 0 720 140" preserveAspectRatio="none">
        <defs>
            <linearGradient id="pm-sg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.30"/>
                <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
            </linearGradient>
        </defs>
        <line x1="0" y1="35"  x2="720" y2="35"  stroke="var(--hairline-2)" stroke-dasharray="2 4"/>
        <line x1="0" y1="70"  x2="720" y2="70"  stroke="var(--hairline-2)" stroke-dasharray="2 4"/>
        <line x1="0" y1="105" x2="720" y2="105" stroke="var(--hairline-2)" stroke-dasharray="2 4"/>
        <path d="${fillPts}" fill="url(#pm-sg)"/>
        <path d="${linePts}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>
    </svg>`;
}

/**
 * Build the recent events log HTML from usage events array.
 */
function _buildEventsLog(events) {
    if (!events || !events.length) {
        return '<div class="m-event"><span class="t">—</span><span class="dot"></span><span class="msg">No recent events</span><span class="v"></span></div>';
    }
    return events.slice(0, 10).map(ev => {
        const ts = ev.ts ? new Date(ev.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—';
        const model = ev.model_id || ev.model || '';
        const toks = ev.tokens_total ? _fmtTokens(ev.tokens_total) + ' tok' : '';
        const cost = ev.cost_usd ? _fmtCost(ev.cost_usd) : '';
        const val = [toks, cost].filter(Boolean).join(' · ') || '—';
        const msgParts = [model, ev.session_id ? 'sess:' + ev.session_id.slice(0, 8) : ''].filter(Boolean);
        return `<div class="m-event good">
            <span class="t">${_esc(ts)}</span>
            <span class="dot"></span>
            <span class="msg">${_esc(msgParts.join(' · ') || 'event')}</span>
            <span class="v">${_esc(val)}</span>
        </div>`;
    }).join('');
}

/**
 * Extract cumulative bucket helpers from STATE.cumulativeMap entry.
 * Entry shape: { lifetime: {...}, month_YYYY-MM: {...}, year_YYYY: {...} }
 */
function _cumBucket(cumData, type) {
    if (!cumData) return null;
    if (type === 'lifetime') return cumData.lifetime || null;
    if (type === 'month') {
        const now = new Date();
        const key = `month_${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
        return cumData[key] || null;
    }
    if (type === 'year') {
        const key = `year_${new Date().getFullYear()}`;
        return cumData[key] || null;
    }
    return null;
}

/** Sum all token types in a cumulative bucket. */
function _bucketTotalTokens(bucket) {
    if (!bucket) return 0;
    return (bucket.tokens_input || 0) + (bucket.tokens_output || 0) +
           (bucket.tokens_cache_read || 0) + (bucket.tokens_cache_create || 0) +
           (bucket.tokens_reasoning || 0);
}

/**
 * Build the Overview pane HTML string.
 *
 * @param {object} entry - Fleet entry from STATE.fleet
 * @param {object|null} cumData - Cumulative data row from STATE.cumulativeMap
 * @param {Array} heatmapCells - Flat array of 168 heatmap values (7 days × 24 hours)
 * @param {Array} recentEvents - Array of usage event objects
 * @returns {string} HTML string
 */
export function buildOverviewPane(entry, cumData, heatmapCells, recentEvents) {
    const critical = entry.critical_gauge || {};
    const allCards = [critical, ...(entry.secondary_limits || [])].filter(Boolean);
    const isPayg = critical.is_unlimited || (!critical.limit_value && !critical.pct_used);

    // Hero gauge values
    const pctUsed = critical.pct_used != null
        ? Math.round(critical.pct_used)
        : (critical.used_value && critical.limit_value
            ? Math.round((critical.used_value / critical.limit_value) * 100)
            : null);
    const used = pctUsed ?? 0;
    const status = used >= 90 ? 'crit' : used >= 70 ? 'warn' : 'good';

    // Glide path: fraction of window elapsed
    let glide = 50;
    if (critical.reset_at) {
        const now = Date.now();
        const reset = new Date(critical.reset_at).getTime();
        const windowMs = (critical.window_seconds || 604800) * 1000;
        const elapsed = Math.max(0, windowMs - (reset - now));
        glide = Math.round((elapsed / windowMs) * 100);
    }

    const ahead = used > glide + 4;
    const behind = used < glide - 4;
    const glideNote = ahead
        ? `<span class="ahead">↑ ${used - glide}% ahead of pace</span>`
        : behind
            ? `<span class="ontrack">✓ ${glide - used}% under pace</span>`
            : `<span class="ontrack">✓ on glide path</span>`;

    // Countdown / burn rate
    const resetIn = critical.reset_in || '—';
    const windowLabel = critical.window_type ? critical.window_type.replace(/_/g, ' ') : '—';
    const usedAbs = critical.used_value != null && critical.limit_value
        ? `${_fmtTokens(critical.used_value)} / ${_fmtTokens(critical.limit_value)} tok`
        : (critical.pct_used != null ? `${used}% used` : '—');
    const burnRate = critical.burn_rate ? `${_fmtTokens(critical.burn_rate)} tok/min` : '—';
    const heroName = critical.service_name
        ? `${critical.service_name} · ${critical.account_label || critical.account_id || ''}`
        : (critical.account_label || critical.account_id || entry.account_id || '—');
    const heroLabel = isPayg ? 'Monthly billing' : 'Critical gauge · most restrictive';

    // KPIs from cumulative data (entry keyed by "lifetime", "month_YYYY-MM", "year_YYYY")
    const monthBucket    = _cumBucket(cumData, 'month');
    const yearBucket     = _cumBucket(cumData, 'year');
    const lifetimeBucket = _cumBucket(cumData, 'lifetime');
    const monthTokNum = _bucketTotalTokens(monthBucket);
    const monthTok   = monthTokNum > 0 ? _fmtTokens(monthTokNum) + ' tok' : '—';
    const monthCost  = _fmtCost(monthBucket?.cost_usd ?? null);
    const ytdCost    = _fmtCost(yearBucket?.cost_usd ?? null);
    const lifetimeTokNum = _bucketTotalTokens(lifetimeBucket);
    const lifetimeTok = lifetimeTokNum > 0 ? _fmtTokens(lifetimeTokNum) + ' tok' : '—';
    const lifetimeCost = _fmtCost(lifetimeBucket?.cost_usd ?? null);

    // Sidecar rows
    const sidecarRows = _buildSidecarRows(entry);
    const totalDelta = sidecarRows.reduce((a, r) => a + r.deltaRaw, 0) || 1;
    const sidecarCount  = sidecarRows.length;
    const healthyCount  = sidecarRows.filter(r => r.status === 'good').length;
    const lastPush = sidecarRows.length ? (sidecarRows[0].ago) : '—';

    const stackBar = sidecarRows.map((r, i) => {
        const share = (r.deltaRaw / totalDelta) * 100;
        const hue = _scHue(r.id, i);
        const cols = Math.max(1, Math.round(share / 5));
        return `<span style="grid-column: span ${cols}; background: oklch(0.62 0.15 ${hue})"></span>`;
    }).join('');

    const sideRowsHtml = sidecarRows.map((r, i) => {
        const share = (r.deltaRaw / totalDelta) * 100;
        const hue = _scHue(r.id, i);
        return `<div class="m-side-row">
            <span class="swatch" style="background: oklch(0.62 0.15 ${hue})"></span>
            <span class="nm"><span class="os">${_osGlyph(r.os)}</span>${_esc(r.name)}<span class="ago">· ${_esc(r.ago)}</span></span>
            <span class="delta">${_esc(r.delta)}</span>
            <span class="cost">${_esc(r.cost)}</span>
            <span class="pct">${share.toFixed(0)}%</span>
            <span class="bar"><i style="width:${share.toFixed(0)}%; background: oklch(0.62 0.15 ${hue})"></i></span>
        </div>`;
    }).join('') || '<div class="m-event"><span class="t">—</span><span class="dot"></span><span class="msg">No sidecars yet</span><span class="v"></span></div>';

    // Model mix
    const mix = _buildModelMix(entry, cumData);
    const donutHtml = _donutSvg(mix);
    const mixLegendHtml = mix.map(s => `
        <div class="it">
            <i style="background: oklch(0.62 0.16 ${s.color})"></i>
            <span class="name">${_esc(s.name)}</span>
            <span class="pct">${s.share}%</span>
            <span class="tok">${_esc(s.tok)}</span>
        </div>`).join('') || '<div style="color:var(--ink-3);font-size:10px;">No model data yet</div>';

    // Sparkline
    const sparkHtml = _buildSparkSvg(heatmapCells, '24h');

    // Events log
    const eventsHtml = _buildEventsLog(recentEvents);

    return `
    <!-- HERO -->
    <div class="m-hero">
        <div>
            <div class="label">${_esc(heroLabel)}</div>
            <div class="name">${_esc(heroName)}</div>
            <div class="pct">${used}<em>%</em></div>
            <div class="abs">${_esc(usedAbs)}</div>
            <div class="gbar ${status}">
                <div class="fill" style="width:${used}%"></div>
                <div class="glide" style="left:${glide}%"></div>
            </div>
            <div class="glide-foot">
                <span>glide-path target <b>${glide}%</b></span>
                ${glideNote}
            </div>
        </div>
        <div class="countdown">
            <div class="label">Window resets in</div>
            <div class="big">${_esc(resetIn)}</div>
            <div class="sub">${_esc(windowLabel)}</div>
            <div class="stat"><span>burn rate</span><b>${_esc(burnRate)}</b></div>
            <div class="stat"><span>headroom</span><b>${Math.max(0, 100 - used)}%</b></div>
        </div>
    </div>

    <!-- KPIs -->
    <div class="m-kpis">
        <div class="kpi">
            <div class="k">This period</div>
            <div class="v">${_esc(monthTok)}</div>
        </div>
        <div class="kpi kpi-cost">
            <div class="k">Period cost</div>
            <div class="v">${_esc(monthCost)}</div>
            <div class="d">${_esc(ytdCost)} YTD</div>
        </div>
        <div class="kpi">
            <div class="k">Sidecars</div>
            <div class="v">${sidecarCount}<em>active · ${healthyCount} healthy</em></div>
            <div class="d">last push ${_esc(lastPush)}</div>
        </div>
        <div class="kpi">
            <div class="k">Lifetime</div>
            <div class="v">${_esc(lifetimeTok)}</div>
            <div class="d">${_esc(lifetimeCost)}</div>
        </div>
    </div>

    <!-- MODEL MIX -->
    <div class="m-block">
        <div class="head">
            <h4>Model mix · this period</h4>
            <span class="meta">${_esc(monthTok)} total</span>
        </div>
        <div class="m-mix">
            ${donutHtml}
            <div class="m-mix-legend">
                ${mixLegendHtml}
            </div>
        </div>
    </div>

    <!-- SIDECAR ATTRIBUTION -->
    <div class="m-block">
        <div class="head">
            <h4>Sidecar attribution · this window</h4>
            <span class="meta">${sidecarCount} machines</span>
        </div>
        <div class="m-side-stack">
            <span style="background:var(--surface-3)"></span>
            ${stackBar}
        </div>
        <div class="m-side">
            ${sideRowsHtml}
        </div>
    </div>

    <!-- SPARKLINE TABS -->
    <div class="m-spark" id="pm-spark-wrap">
        <div class="m-spark-head">
            <h4>Usage history</h4>
            <span class="m-spark-tabs" id="pm-spark-tabs">
                <button data-range="24h" class="on">24h</button>
                <button data-range="7d">7d</button>
                <button data-range="30d">30d</button>
            </span>
        </div>
        ${sparkHtml}
    </div>

    <!-- EVENTS LOG -->
    <div class="m-block">
        <div class="head">
            <h4>Recent events</h4>
            <span class="meta">last 10</span>
        </div>
        <div class="m-events">
            ${eventsHtml}
        </div>
    </div>
    `;
}

/**
 * Wire spark tab click → rebuild the sparkline SVG.
 * Call after injecting overview pane HTML into the DOM.
 */
export function wireOverviewSparkTabs(heatmapCells) {
    const tabs = document.getElementById('pm-spark-tabs');
    if (!tabs) return;
    tabs.addEventListener('click', e => {
        const btn = e.target.closest('button');
        if (!btn) return;
        tabs.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === btn));
        const wrap = document.getElementById('pm-spark-wrap');
        if (!wrap) return;
        const oldSvg = wrap.querySelector('svg');
        if (oldSvg) {
            const newSvg = document.createElement('div');
            newSvg.innerHTML = _buildSparkSvg(heatmapCells, btn.dataset.range);
            const svgEl = newSvg.querySelector('svg');
            if (svgEl) oldSvg.replaceWith(svgEl);
        }
    });
}
