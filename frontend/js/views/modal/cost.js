/**
 * Cost pane builder for the provider detail modal.
 *
 * Data sources:
 *   - entry   : fleet entry (for sidecar attribution)
 *   - cumData : cumulative row from STATE.cumulativeMap
 *   - allCumulative : full fetchCumulative() list for monthly bars (if available)
 */

import { escapeHTML as _esc } from '../../utils/html.js';
import { formatCost as _fmtCost, formatTokens as _fmtTokens } from '../../utils/format.js';

function _osGlyph(os) {
    if (!os) return '◈ ';
    const l = os.toLowerCase();
    if (l.includes('darwin') || l.includes('mac')) return '⌘ ';
    if (l.includes('win')) return '⊞ ';
    if (l.includes('linux')) return '🐧 ';
    return '◈ ';
}

function _scHue(id, idx) {
    let hash = 0;
    for (const ch of String(id)) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
    return Math.abs(hash) % 360 || (idx * 47 + 60) % 360;
}

/** Build sidecar cost rows from entry.sidecar_contributions. */
function _buildSidecarCostRows(entry) {
    const contributions = entry.sidecar_contributions || {};
    const rows = [];
    for (const [sid, stats] of Object.entries(contributions)) {
        rows.push({
            id: sid,
            name: sid,
            os: stats.os || '',
            costRaw: stats.cost_usd || 0,
            cost: _fmtCost(stats.cost_usd || 0),
            tokRaw: (stats.tokens_input || 0) + (stats.tokens_output || 0),
            delta: '+' + _fmtTokens((stats.tokens_input || 0) + (stats.tokens_output || 0)),
        });
    }
    rows.sort((a, b) => b.costRaw - a.costRaw);
    return rows;
}

/** Extract cumulative bucket from cumData entry (same structure as overview.js). */
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

function _bucketTotalTokens(bucket) {
    if (!bucket) return 0;
    return (bucket.tokens_input || 0) + (bucket.tokens_output || 0) +
           (bucket.tokens_cache_read || 0) + (bucket.tokens_cache_create || 0) +
           (bucket.tokens_reasoning || 0);
}

/**
 * Build a monthly cost bar chart SVG from cumData.
 * Scans all keys matching "month_YYYY-MM" in the current year.
 *
 * @param {object} cumData - Cumulative data row
 * @returns {{ svgHtml: string, ytdStr: string }}
 */
function _buildMonthlyCostSvg(cumData) {
    const MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
    const currentYear    = new Date().getFullYear();
    const currentMonthIdx = new Date().getMonth();

    // Scan cumData for month_YYYY-MM keys matching current year
    const monthly = new Array(12).fill(0);
    if (cumData) {
        for (const [k, v] of Object.entries(cumData)) {
            const m = k.match(/^month_(\d{4})-(\d{2})$/);
            if (!m) continue;
            if (parseInt(m[1], 10) !== currentYear) continue;
            const mIdx = parseInt(m[2], 10) - 1;
            if (mIdx >= 0 && mIdx < 12 && v && v.cost_usd) {
                monthly[mIdx] = v.cost_usd;
            }
        }
    }

    const maxVal = Math.max(...monthly, 0.01);
    const ytdStr = _fmtCost(monthly.reduce((s, v) => s + v, 0));

    const svgHtml = `<svg viewBox="0 0 720 180" preserveAspectRatio="none" style="width:100%;height:180px;display:block">
        <line x1="0" y1="45"  x2="720" y2="45"  stroke="var(--hairline-2)" stroke-dasharray="2 4"/>
        <line x1="0" y1="90"  x2="720" y2="90"  stroke="var(--hairline-2)" stroke-dasharray="2 4"/>
        <line x1="0" y1="135" x2="720" y2="135" stroke="var(--hairline-2)" stroke-dasharray="2 4"/>
        ${MONTHS.map((m, i) => {
            const w = 720 / 12 - 8;
            const x = i * (720 / 12) + 4;
            const h = Math.max(0, (monthly[i] / maxVal) * 150);
            const y = 170 - h;
            const isCur = i === currentMonthIdx;
            return `<rect x="${x}" y="${y.toFixed(1)}" width="${w}" height="${h.toFixed(1)}" fill="${isCur ? 'var(--accent)' : 'color-mix(in srgb, var(--accent) 35%, transparent)'}"/><text x="${(x + w / 2).toFixed(1)}" y="178" text-anchor="middle" font-family="var(--mono)" font-size="9" fill="var(--ink-3)" letter-spacing="0.1em">${m}</text>`;
        }).join('')}
    </svg>`;
    return { svgHtml, ytdStr };
}

/**
 * Build the Cost pane HTML string.
 *
 * @param {object} entry - Fleet entry from STATE.fleet
 * @param {object|null} cumData - Cumulative data row from STATE.cumulativeMap
 * @returns {string} HTML string
 */
export function buildCostPane(entry, cumData) {
    const monthBucket = _cumBucket(cumData, 'month');
    const yearBucket  = _cumBucket(cumData, 'year');

    const periodCost  = _fmtCost(monthBucket?.cost_usd ?? null);
    const ytdCost     = _fmtCost(yearBucket?.cost_usd ?? null);
    const monthTokNum = _bucketTotalTokens(monthBucket);
    const monthTok    = monthTokNum > 0 ? _fmtTokens(monthTokNum) + ' tokens' : '—';
    const yearTokNum  = _bucketTotalTokens(yearBucket);
    const yearlyTok   = yearTokNum > 0 ? _fmtTokens(yearTokNum) + ' tokens' : '—';

    // Forecast EoM: linear projection using day of month
    const today = new Date();
    const dayOfMonth = today.getDate();
    const daysInMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
    const curSpend = monthBucket?.cost_usd ?? 0;
    const forecastEoM = dayOfMonth > 0 ? curSpend * (daysInMonth / dayOfMonth) : curSpend;
    const forecastStr = _fmtCost(forecastEoM);
    const forecastNote = dayOfMonth > 0
        ? `×${(daysInMonth / dayOfMonth).toFixed(1)} projection`
        : 'full month';

    const { svgHtml, ytdStr } = _buildMonthlyCostSvg(cumData);
    const yearLabel = today.getFullYear();

    // Sidecar cost attribution
    const sidecarRows = _buildSidecarCostRows(entry);
    const totalCost = sidecarRows.reduce((s, r) => s + r.costRaw, 0) || 1;

    const sideRowsHtml = sidecarRows.map((r, i) => {
        const share = (r.costRaw / totalCost) * 100;
        const hue = _scHue(r.id, i);
        return `<div class="m-side-row">
            <span class="swatch" style="background: oklch(0.62 0.15 ${hue})"></span>
            <span class="nm"><span class="os">${_osGlyph(r.os)}</span>${_esc(r.name)}</span>
            <span class="delta">${_esc(r.cost)}</span>
            <span class="cost">${_esc(r.delta)}</span>
            <span class="pct">${share.toFixed(0)}%</span>
            <span class="bar"><i style="width:${share.toFixed(0)}%; background: oklch(0.62 0.15 ${hue})"></i></span>
        </div>`;
    }).join('') || '<div class="m-event"><span class="t">—</span><span class="dot"></span><span class="msg">No cost attribution yet</span><span class="v"></span></div>';

    return `
    <!-- COST HERO -->
    <div class="m-hero" style="grid-template-columns:1fr 1fr 1fr">
        <div>
            <div class="label">Period spend</div>
            <div class="pct">${_esc(periodCost)}</div>
            <div class="abs">${_esc(monthTok)}</div>
        </div>
        <div>
            <div class="label">Forecast EoM</div>
            <div class="pct">${_esc(forecastStr)}</div>
            <div class="abs">${_esc(forecastNote)}</div>
        </div>
        <div>
            <div class="label">YTD spend</div>
            <div class="pct">${_esc(ytdCost)}</div>
            <div class="abs">${_esc(yearlyTok)}</div>
        </div>
    </div>

    <!-- MONTHLY BARS -->
    <div class="m-block m-cost-block">
        <div class="head">
            <h4>Monthly spend · ${yearLabel}</h4>
            <span class="meta">${_esc(ytdStr)} YTD</span>
        </div>
        ${svgHtml}
    </div>

    <!-- SIDECAR COST BREAKDOWN -->
    <div class="m-block m-cost-block">
        <div class="head"><h4>Cost by sidecar · this period</h4></div>
        <div class="m-side">
            ${sideRowsHtml}
        </div>
    </div>
    `;
}
