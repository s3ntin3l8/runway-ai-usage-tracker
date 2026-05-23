/**
 * Overview pane builder for the provider detail modal.
 *
 * Data sources:
 *   - entry          : fleet entry (critical_gauge, secondary_limits, sidecar_contributions)
 *   - cumData        : fetchCumulative() row for this account, from STATE.cumulativeMap
 *   - heatmap        : fetchHeatmap() response (for sparkline rendering)
 *   - recentSessions : fetchSessions({sort_by:'recent'}) response (3 most recent sessions)
 */

import { providerDisplayLabel } from '../../components.js';
import { formatLocalTime } from '../../utils/tz.js';
import { escapeHTML as _esc } from '../../utils/html.js';
import { formatTokens as _fmtTokens, formatCost as _fmtCost } from '../../utils/format.js';
import { buildSessionCard } from './usage.js';

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
        // Fall back to by_model on the fleet entry cards. Card by_model can
        // come in two shapes:
        //   { tokens_total: <number>, cost_usd, msgs }                          -- flat
        //   { tokens: { input, output, total, ... }, cost, msgs }               -- nested
        // The nested form is what OpenCode and other token-rich collectors
        // emit. Reading `stats.tokens` directly when it's an object would
        // concatenate "[object Object]"; pull the total off it instead.
        const allCards = [entry.critical_gauge, ...(entry.secondary_limits || [])].filter(Boolean);
        for (const c of allCards) {
            if (!c.by_model) continue;
            for (const [mdl, stats] of Object.entries(c.by_model)) {
                if (!byModel[mdl]) byModel[mdl] = { tok: 0, cost: 0, msgs: 0 };
                let tok = 0;
                if (typeof stats.tokens_total === 'number') {
                    tok = stats.tokens_total;
                } else if (stats.tokens && typeof stats.tokens === 'object') {
                    tok = Number(stats.tokens.total ?? 0)
                        || (Number(stats.tokens.input ?? 0)
                            + Number(stats.tokens.output ?? 0)
                            + Number(stats.tokens.cache_read ?? 0)
                            + Number(stats.tokens.cache_create ?? 0)
                            + Number(stats.tokens.reasoning ?? 0));
                } else if (typeof stats.tokens === 'number') {
                    tok = stats.tokens;
                }
                byModel[mdl].tok  += tok;
                byModel[mdl].cost += Number(stats.cost_usd ?? stats.cost ?? 0);
                byModel[mdl].msgs += Number(stats.msgs ?? 0);
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
 * Build sidecar rows from entry.sidecar_contributions, augmented with any
 * sidecars that own a card in the entry but emit no usage events (e.g.
 * antigravity is quota-only — no rollup contributions, but cards still
 * carry a sidecar_id).
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
    const seen = new Set(rows.map(r => r.id));
    const cardSources = [entry.critical_gauge, ...(entry.secondary_limits || [])];
    for (const c of cardSources) {
        const sid = c?.sidecar_id;
        if (!sid || seen.has(sid)) continue;
        seen.add(sid);
        rows.push({
            id: sid,
            name: sid,
            os: '',
            deltaRaw: 0,
            delta: '—',
            costRaw: 0,
            cost: '—',
            status: 'good',
            ago: c?.updated_at ? _fmtAgo(c.updated_at) : '—',
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

// Module-level state for the overview sparkline hover — updated on each tab switch.
let _overviewPoints = [];
let _overviewRange  = '24h';

/**
 * Extract the aggregate (model_id=null/''/"") series from a fetchHistoryChart response.
 * Falls back to series[0] if no aggregate exists.
 * @param {object|null} chartData - fetchHistoryChart() response
 * @returns {Array<{ts: string, pct_used: number}>}
 */
function _extractOverviewPoints(chartData) {
    if (!chartData || !chartData.series || !chartData.series.length) return [];
    const agg = chartData.series.find(s => !s.model_id || s.model_id === '') || chartData.series[0];
    return agg?.points || [];
}

/**
 * Build a quota-percentage sparkline SVG from points: [{ts, pct_used}].
 * Y scale is fixed 0–100%. Grid lines at 75/50/25%.
 * @returns {{ svgHtml: string, yTicks: Array<{y: string, label: string}> }}
 */
function _buildQuotaSparkSvg(points) {
    if (!points || !points.length) {
        return {
            svgHtml: '<svg id="pm-spark-svg" viewBox="0 0 720 140" preserveAspectRatio="none"><text x="360" y="75" text-anchor="middle" font-size="10" fill="var(--ink-3)" font-family="var(--mono)">No data</text></svg>',
            yTicks: [],
        };
    }

    const w = 720, h = 140;
    // Fixed 0–100 % scale with 6px top and bottom margin
    const toY = pct => h - (Math.max(0, Math.min(100, pct)) / 100) * (h - 12) - 6;

    const xStep = w / (points.length - 1 || 1);
    const svgPts = points.map((p, i) => ({ x: i * xStep, y: toY(p.pct_used || 0) }));
    const linePts = svgPts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ');
    const fillPts = `M 0 ${h} ${svgPts.map(p => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')} L${w} ${h} Z`;

    // Grid at 75%, 50%, 25%
    const yTicks = [75, 50, 25].map(pct => ({
        y:     toY(pct).toFixed(1),
        label: pct + '%',
    }));

    const svgHtml = `<svg id="pm-spark-svg" viewBox="0 0 720 140" preserveAspectRatio="none">
        <defs>
            <linearGradient id="pm-sg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.30"/>
                <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
            </linearGradient>
        </defs>
        ${yTicks.map(t => `<line x1="0" y1="${t.y}" x2="720" y2="${t.y}" stroke="var(--hairline-2)" stroke-dasharray="2 4"/>`).join('\n        ')}
        <path d="${fillPts}" fill="url(#pm-sg)"/>
        <path d="${linePts}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>
    </svg>`;

    return { svgHtml, yTicks };
}

/**
 * Build X-axis label strip HTML from the points array.
 * Picks 5 evenly-spaced points and formats their timestamps by range.
 * @param {Array<{ts: string, pct_used: number}>} points
 * @param {string} range - '24h' | '7d' | '30d'
 * @returns {string} HTML string
 */
function _buildXLabelsFromPoints(points, range) {
    if (!points || !points.length) {
        return `<div class="m-spark-x-axis" id="pm-x-axis"></div>`;
    }
    const n = 5;
    const labels = [];
    for (let i = 0; i < n; i++) {
        const idx = Math.round((i / (n - 1)) * (points.length - 1));
        const pt  = points[Math.min(idx, points.length - 1)];
        const d   = new Date(pt.ts);
        let label = '';
        if (range === '24h') {
            label = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
        } else if (range === '7d') {
            label = d.toLocaleDateString([], { weekday: 'short' });
        } else {
            label = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
        }
        labels.push(label);
    }
    return `<div class="m-spark-x-axis" id="pm-x-axis">${
        labels.map(l => `<span>${_esc(l)}</span>`).join('')
    }</div>`;
}

/**
 * Build the recent sessions section from sessions array.
 * Returns { cardsHtml, meta } where meta is the aggregate summary string.
 */
function _buildRecentSessions(sessions) {
    if (!sessions || !sessions.length) {
        return {
            cardsHtml: '<div class="m-event"><span class="t">—</span><span class="dot"></span><span class="msg">No recent sessions</span><span class="v"></span></div>',
            meta: '—',
        };
    }
    const totalTok  = sessions.reduce((a, s) => a + (s.tokens_total || 0), 0);
    const totalCost = sessions.reduce((a, s) => a + (s.cost_usd || 0), 0);
    const avgCache  = Math.round(
        sessions.reduce((a, s) => a + (s.cache_pct || 0), 0) / sessions.length
    );
    const meta = [
        `${sessions.length} session${sessions.length !== 1 ? 's' : ''}`,
        totalTok  ? _fmtTokens(totalTok) + ' tok' : null,
        totalCost ? _fmtCost(totalCost)  : null,
        avgCache > 0 ? `${avgCache}% cached` : null,
    ].filter(Boolean).join(' · ');

    return { cardsHtml: sessions.map(buildSessionCard).join(''), meta };
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
export function buildOverviewPane(entry, cumData, heatmapCells, recentSessions) {
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
    // Prefer a live countdown computed from reset_at — `critical.reset` is
    // sometimes a duration label (e.g. OpenCode emits "7d") rather than a
    // countdown, which would render as "Window resets in 7d weekly" — wrong.
    let resetIn = critical.reset_in || '—';
    if (critical.reset_at) {
        try {
            const ms = new Date(critical.reset_at).getTime() - Date.now();
            if (Number.isFinite(ms) && ms > 0) {
                const totalMin = Math.floor(ms / 60000);
                const d = Math.floor(totalMin / (60 * 24));
                const h = Math.floor((totalMin % (60 * 24)) / 60);
                const m = totalMin % 60;
                resetIn = d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
            } else if (Number.isFinite(ms) && ms <= 0) {
                resetIn = 'now';
            }
        } catch (_) {
            // fall through to critical.reset
        }
    }
    if (resetIn === '—') resetIn = critical.reset || '—';
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

    const { cardsHtml: sessionCardsHtml, meta: sessionsMeta } = _buildRecentSessions(recentSessions);

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
            <h4>Model mix · this month</h4>
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

    <!-- RECENT SESSIONS -->
    <div class="m-block">
        <div class="head">
            <h4>Recent sessions</h4>
            <span class="meta">${_esc(sessionsMeta)}</span>
        </div>
        <div class="m-events">
            ${sessionCardsHtml}
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

