// Dashboard view module - lazy loaded via dynamic import
import { fetchLimits, fetchForecast, fetchHistoryRaw, fetchHistoryChart, fetchHistoryWindowDetail, fetchUsageFleet, fetchCumulative } from '../api.js';
import { STATE } from '../state.js';
import { buildHorizonCard, buildCardModalContent, providerDisplayLabel, buildFleetCommanderCard } from '../components.js';
import { cardKey, applyOrder } from '../layout.js';
import { escapeHTML } from '../utils/html.js';
import { openProviderModal, initProviderModal } from './modal/index.js';

let loadDataGeneration = 0;
let _searchQuery = '';

function _forecastSeriesKey(entry) {
    return [
        entry.provider_id || '',
        entry.account_id || '',
        entry.service_name || '',
        entry.variant || '',
        entry.model_id || '',
        entry.window_type || '',
        entry.unit_type || '',
    ].join('||');
}

/** Filter cards by STATE.activeFilter and live search query. */
export function applyFilters(data) {
    const f = STATE.activeFilter;
    return data.filter(item => {
        if (f && item[f.dimension] !== f.value) return false;
        if (_searchQuery) {
            const q = _searchQuery.toLowerCase();
            const haystack = [item.service_name, item.account_label, item.provider_id].join(' ').toLowerCase();
            if (!haystack.includes(q)) return false;
        }
        return true;
    });
}

/** Map a card's health enum to the light's CSS class. */
function _lightBucket(health) {
    if (health === 'critical') return 'crit';
    if (health === 'warning')  return 'warn';
    if (health === 'good' || health === 'unlimited') return '';
    return 'off';
}

/** Render the v4 fleet strip: title · 28-style light grid · bucket counts. */
function renderFleetHealth(cards) {
    const lightsEl = document.getElementById('fleet-lights');
    const subEl    = document.getElementById('fleet-sub');
    const countsEl = document.getElementById('fleet-counts');
    if (!lightsEl) return;

    const nominal  = cards.filter(c => c.health === 'good' || c.health === 'unlimited').length;
    const nWarn    = cards.filter(c => c.health === 'warning').length;
    const nCrit    = cards.filter(c => c.health === 'critical').length;
    const nErr     = cards.filter(c => c.error_type).length;
    const providers = new Set(cards.map(c => c.provider_id).filter(Boolean)).size;
    const accounts  = new Set(cards.map(c => `${c.provider_id}|${c.account_id}`).filter(s => s !== '|')).size;

    // Subtitle: "23 / 28 nominal · 7 providers · 12 accounts"
    if (subEl) {
        subEl.textContent = `${nominal} / ${cards.length} nominal · ${providers} provider${providers !== 1 ? 's' : ''} · ${accounts} account${accounts !== 1 ? 's' : ''}`;
    }

    // Lights: dynamic columns, one cell per card. Tooltip styled to match v4
    // (provider · account / status · service / reset).
    lightsEl.style.gridTemplateColumns = `repeat(${Math.max(1, cards.length)}, 1fr)`;
    lightsEl.innerHTML = cards.map(c => {
        const cls    = _lightBucket(c.health);
        const status = (c.health || 'unknown').toUpperCase();
        const pid    = providerDisplayLabel(c.provider_id || '');
        const acct   = c.account_label || c.account_id || '';
        const svc    = c.service_name || c.window_type || '—';
        const pct    = c.pct_used != null ? Math.round(100 - c.pct_used) :
            (c.used_value != null && c.limit_value ? Math.round(100 - c.used_value / c.limit_value * 100) : null);
        const reset  = _formatResetIn(c.reset_at);
        return `
            <div class="tooltip-container" style="height:100%;">
                <i class="${cls}" style="height:100%;width:100%;"></i>
                <div class="tooltip" style="bottom:100%;margin-bottom:8px;z-index:300;transform:translateX(50%);right:50%;min-width:200px;">
                    <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;">
                        <div>
                            <div style="font-size:11px;font-weight:700;color:var(--text);">${escapeHTML(pid)}</div>
                            <div style="font-size:9px;color:var(--text-dim);">${escapeHTML(acct)}</div>
                        </div>
                        <div style="font-size:14px;font-weight:700;color:var(--${cls || 'good'});font-variant-numeric:tabular-nums;">${pct != null ? pct + '%' : '—'}</div>
                    </div>
                    <div style="height:1px;background:var(--hairline);margin:6px 0;"></div>
                    <div style="display:flex;justify-content:space-between;gap:12px;font-size:9px;">
                        <span style="color:var(--text-dim);">${escapeHTML(svc)}</span>
                        <span style="color:var(--${cls || 'good'});font-weight:700;">${escapeHTML(status)}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;gap:12px;font-size:9px;margin-top:4px;">
                        <span style="color:var(--text-dim);">resets</span>
                        <span style="color:var(--text);font-weight:700;">${escapeHTML(reset)}</span>
                    </div>
                </div>
            </div>`;
    }).join('');

    // Bucket counts on the right side of the strip
    if (countsEl) {
        // Observed-health counts (existing)
        const parts = [`<span><b>${nominal}</b>OK</span>`];
        if (nWarn) parts.push(`<span class="warn"><b>${nWarn}</b>WARN</span>`);
        if (nCrit) parts.push(`<span class="crit"><b>${nCrit}</b>CRIT</span>`);
        if (nErr)  parts.push(`<span class="err"><b>${nErr}</b>ERR</span>`);

        // Forecast-status counts — independent signal from observed health
        const fMap = STATE.forecastMap;
        if (fMap?.size) {
            const nRisk    = cards.filter(c => fMap.get(_forecastKeyForCard(c))?.status === 'risk').length;
            const nExhaust = cards.filter(c => fMap.get(_forecastKeyForCard(c))?.status === 'exhausted').length;
            if (nRisk)    parts.push(`<span class="crit" title="Projected to hit 100% before reset"><b>${nRisk}</b>PROJ-RISK</span>`);
            if (nExhaust) parts.push(`<span class="crit" title="Already projected exhausted"><b>${nExhaust}</b>EXHAUST</span>`);

            const genAt = STATE.forecastGeneratedAt;
            if (genAt) {
                const t = new Date(genAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                parts.push(`<span style="color:var(--text-dim);font-size:9px;">forecast ${t}</span>`);
            }
        }
        countsEl.innerHTML = parts.join('');
    }
}

function _formatResetIn(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return '—';
    const ms = d - Date.now();
    if (ms <= 0) return 'now';
    const totalMin = Math.round(ms / 60000);
    const days = Math.floor(totalMin / (60 * 24));
    const hours = Math.floor((totalMin % (60 * 24)) / 60);
    const mins = totalMin % 60;
    if (days >= 1) return `${days}d ${hours}h`;
    if (hours >= 1) return `${hours}h ${String(mins).padStart(2, '0')}m`;
    return `${mins}m`;
}

/** Stable join of card identity fields → forecast lookup key. */
function _forecastKeyForCard(c) {
    return [
        c.provider_id || '',
        c.account_id || '',
        c.service_name || '',
        c.variant || '',
        c.model_id || '',
        c.window_type || '',
        c.unit_type || '',
    ].join('||');
}

/** Format a numeric value compactly with a unit suffix matching the card. */
function _formatUnit(val, unitType) {
    if (val == null || isNaN(val)) return '—';
    const unit = unitType === 'messages' ? 'msg'
        : unitType === 'currency' ? '$'
        : unitType === 'requests' ? 'req'
        : 'tok';
    const fmt = v => v >= 1e9 ? (v / 1e9).toFixed(1) + 'B'
        : v >= 1e6 ? (v / 1e6).toFixed(1) + 'M'
        : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K'
        : String(Math.round(v));
    return unit === '$' ? `$${fmt(val)}` : `${fmt(val)} ${unit}`;
}

/** Format "Lands in Xh" / "Xd Yh" / "<1h" / "LANDED". */
function _formatLandsIn(iso) {
    if (!iso) return 'Lands in —';
    const ms = new Date(iso) - Date.now();
    if (isNaN(ms)) return 'Lands in —';
    if (ms <= 0) return 'LANDED';
    const totalMin = Math.round(ms / 60000);
    if (totalMin < 60) return totalMin < 5 ? 'Lands in <1h' : `Lands in ${totalMin}m`;
    const hours = Math.floor(totalMin / 60);
    const days  = Math.floor(hours / 24);
    if (days >= 1) {
        const remH = hours - days * 24;
        return remH > 0 ? `Lands in ${days}d ${remH}h` : `Lands in ${days}d`;
    }
    return `Lands in ${hours}h`;
}

/** Build the sparkline SVG layer for a crit card from 24h history points.
 *  Returns '' (no <svg>) when there's nothing meaningful to draw. */
function _buildCritSpark(points) {
    if (!points || points.length < 3) return '';
    const vals = points.map(p => Number(p.value ?? p.pct_used ?? 0)).filter(v => !isNaN(v));
    if (vals.length < 3) return '';
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    if (max - min < 0.5) return '';   // essentially flat — skip
    const W = 600, H = 200;
    const xs = vals.map((_, i) => (i / (vals.length - 1)) * W);
    const ys = vals.map(v => H - ((v - min) / (max - min)) * H);
    const linePath = xs.map((x, i) => `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${ys[i].toFixed(1)}`).join(' ');
    const areaPath = `${linePath} L ${W} ${H} L 0 ${H} Z`;

    // Inflection: find steepest segment (max -Δy / Δx since higher value = lower y)
    let inflIdx = vals.length - 2;
    let maxSlope = -Infinity;
    for (let i = 1; i < vals.length; i++) {
        const dy = ys[i - 1] - ys[i];          // positive = rising
        if (dy > maxSlope) { maxSlope = dy; inflIdx = i; }
    }
    const inflX = xs[inflIdx];
    const inflFrac = inflX / W;

    const nowX = xs[xs.length - 1];
    const nowY = ys[ys.length - 1];

    return `
        <svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
            <path class="area" d="${areaPath}" />
            <path class="line" d="${linePath}" />
            <line class="infl" x1="${inflX.toFixed(1)}" y1="0" x2="${inflX.toFixed(1)}" y2="${H}" />
            <circle class="now-ring" cx="${nowX.toFixed(1)}" cy="${nowY.toFixed(1)}" r="5" />
            <circle class="now-dot"  cx="${nowX.toFixed(1)}" cy="${nowY.toFixed(1)}" r="3" />
        </svg>
        <div class="spark-annot tail-lbl">← since reset</div>
        <div class="spark-annot infl-lbl" style="left:${(inflFrac * 100).toFixed(0)}%; top: 2px;"><b>FAST</b></div>`;
}

/** Pull a computable pct_used out of a card-shaped object, or null if missing. */
function _pctUsedOf(c) {
    if (!c) return null;
    if (c.pct_used != null) return c.pct_used;
    if (c.used_value != null && c.limit_value) return (c.used_value / c.limit_value) * 100;
    return null;
}

/** Render a single most-constrained card into the given slot element.
 *  The card's accent color (crit/warn/good) follows the entry's health. */
function _renderCritCard(slotEl, entry, forecastMap, historyByKey, isPrimary) {
    const c = entry.critical_gauge;
    const pct_used  = _pctUsedOf(c);
    const remaining = pct_used != null ? Math.max(0, Math.round(100 - pct_used)) : null;

    // Health → accent class. Anything not crit/warn falls back to the good accent.
    const accentCls = c.health === 'critical' ? 'crit'
        : c.health === 'warning' ? 'warn'
        : 'good';

    const fKey = _forecastKeyForCard(c);
    const fe   = forecastMap?.get(fKey);
    const paceLabel = fe?.status === 'risk' || fe?.status === 'exhausted' ? 'FAST'
        : fe?.status === 'warn' ? 'MODERATE'
        : fe?.status === 'ok' || fe?.status === 'stable' ? 'STABLE'
        : null;

    // Expected % at this point in the window — needs window_start + reset_at from the forecast
    let expectedPct = null;
    let nominalMult = null;
    if (fe?.window_start && fe?.reset_at) {
        const ws = new Date(fe.window_start);
        const re = new Date(fe.reset_at);
        const total = re - ws;
        const elapsed = Date.now() - ws;
        if (total > 0 && elapsed > 0) {
            expectedPct = Math.max(0, Math.min(100, (elapsed / total) * 100));
            if (pct_used != null && expectedPct > 0.5) {
                nominalMult = pct_used / expectedPct;
            }
        }
    }

    const usedPct = pct_used != null ? Math.max(0, Math.min(100, pct_used)) : 0;
    const expPct  = expectedPct != null ? expectedPct : usedPct;

    const used   = c.used_value != null ? c.used_value : c.token_usage?.total;
    const left   = (c.limit_value != null && used != null) ? Math.max(0, c.limit_value - used) : null;

    const headLabel = isPrimary
        ? (accentCls === 'crit' ? 'Most Constrained · projected exhaust' : 'Most Constrained')
        : '2nd Most Constrained';
    const metaParts = [];
    if (paceLabel) metaParts.push(`Pace <b>${paceLabel}</b>`);
    if (nominalMult != null && isFinite(nominalMult)) {
        metaParts.push(`<b>${nominalMult.toFixed(1)}×</b> nominal`);
    }

    const sparkHTML = _buildCritSpark(historyByKey?.get(fKey));

    slotEl.hidden = false;
    slotEl.className = `glass crit-card ${accentCls}`;
    slotEl.innerHTML = `
        <div class="head">
            <div class="lbl">${headLabel}</div>
            <div class="meta">${metaParts.join(' · ')}</div>
        </div>
        <div class="body">
            ${sparkHTML}
            <div class="pct">${remaining != null ? remaining : '—'}<em>%</em></div>
            <div class="who">
                <div class="prov">${escapeHTML(providerDisplayLabel(c.provider_id || ''))} · ${escapeHTML(c.account_label || c.account_id || '')}</div>
                <div class="name">${escapeHTML(_formatLandsIn(fe?.projected_limit_hit_at))}</div>
                <div class="when"><b>${escapeHTML(_formatUnit(used, c.unit_type))}</b> · <b>${escapeHTML(_formatUnit(left, c.unit_type))}</b> left</div>
                ${nominalMult != null ? `<div class="pace">at current pace · <b>${nominalMult.toFixed(1)}× nominal</b></div>` : ''}
            </div>
        </div>
        <div>
            <div class="bar" style="--used:${usedPct.toFixed(1)}%; --exp:${expPct.toFixed(1)}%">
                <div class="fill"></div>
                ${expectedPct != null ? '<div class="pace-mark" title="expected at this point in window"></div>' : ''}
            </div>
            <div class="scale">
                <span class="s-0">0%</span>
                ${expectedPct != null ? `<span style="left:${expPct.toFixed(1)}%">${Math.round(expPct)}%</span>` : ''}
                <span class="s-100">100%</span>
            </div>
        </div>`;
}

/** Render the most-constrained hero slot(s).
 *  - Slot 1 always shows the card with the highest pct_used (any health).
 *  - Slot 2 shows the next most-constrained card *only when slot 1 is critical*.
 *  - Empty state ("All systems nominal") shows when no card has a computable pct_used. */
function renderConstrainedHero(fleet, forecastMap, historyByKey) {
    const slot1 = document.getElementById('crit-1');
    const slot2 = document.getElementById('crit-2');
    const clear = document.getElementById('crit-clear');
    const body  = document.getElementById('hero-body');
    if (!slot1 || !slot2 || !clear || !body) return;

    body.classList.remove('single', 'empty');

    // Eligible: fleet entries with a critical_gauge that has a computable pct_used.
    // Skip unknown/errored cards — those don't represent a meaningful usage signal.
    const eligible = (fleet || []).filter(e => {
        const c = e.critical_gauge;
        if (!c) return false;
        if (c.health === 'unknown' || c.health === 'unlimited') return false;
        if (c.error_type) return false;
        return _pctUsedOf(c) != null;
    });

    if (!eligible.length) {
        slot1.hidden = true;
        slot2.hidden = true;
        clear.hidden = false;
        body.classList.add('empty');
        clear.innerHTML = `
            <div class="clear-inner">
                <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                <div class="clear-label">All systems nominal</div>
            </div>`;
        return;
    }

    // Sort by pct_used desc; tie-break by soonest projected exhaustion.
    const sorted = [...eligible].sort((a, b) => {
        const ap = _pctUsedOf(a.critical_gauge) ?? 0;
        const bp = _pctUsedOf(b.critical_gauge) ?? 0;
        if (bp !== ap) return bp - ap;
        const aHit = forecastMap?.get(_forecastKeyForCard(a.critical_gauge))?.projected_limit_hit_at;
        const bHit = forecastMap?.get(_forecastKeyForCard(b.critical_gauge))?.projected_limit_hit_at;
        if (aHit && bHit) return new Date(aHit) - new Date(bHit);
        if (aHit) return -1;
        if (bHit) return 1;
        return 0;
    });

    clear.hidden = true;
    _renderCritCard(slot1, sorted[0], forecastMap, historyByKey, true);

    // Slot 2 only when slot 1 is critical AND there's another card to show.
    const primaryIsCrit = sorted[0].critical_gauge.health === 'critical';
    if (primaryIsCrit && sorted.length >= 2) {
        _renderCritCard(slot2, sorted[1], forecastMap, historyByKey, false);
    } else {
        slot2.hidden = true;
        body.classList.add('single');
    }
}

/** Render the v4 aggregate card: big remaining %, reset countdown, dashed stat rows. */
function renderAggregateHero(cards, forecastMap) {
    const aggEl = document.getElementById('agg-card');
    if (!aggEl) return;

    const pctOf = c => c.pct_used != null ? c.pct_used
        : (c.used_value != null && c.limit_value ? c.used_value / c.limit_value * 100 : null);

    const eligible = cards.filter(c =>
        !c.error_type && !c.is_unlimited && c.health !== 'unknown' && pctOf(c) != null
    );

    let sumRemaining = 0, sumWeight = 0;
    eligible.forEach(c => {
        const p = pctOf(c);
        if (p == null) return;
        const w = c.limit_value || 1;
        sumRemaining += (100 - p) * w;
        sumWeight    += w;
    });
    const avgRemaining = sumWeight > 0 ? sumRemaining / sumWeight : null;
    const pctInt = avgRemaining != null ? Math.round(avgRemaining) : null;

    // Soonest reset
    const resets = cards.map(c => c.reset_at).filter(Boolean).map(r => new Date(r)).filter(d => !isNaN(d));
    const soonest = resets.length ? new Date(Math.min(...resets)) : null;
    const resetLbl = soonest ? _formatResetIn(soonest.toISOString()) : '—';

    // Fleet pace from forecast statuses
    const statuses = [...(forecastMap?.values() || [])].map(f => f.status).filter(Boolean);
    const fast = statuses.some(s => s === 'risk' || s === 'exhausted');
    const warn = !fast && statuses.some(s => s === 'warn');
    const paceVal = fast ? 'FAST' : warn ? 'MODERATE' : statuses.length ? 'STABLE' : '—';
    const paceCls = fast ? 'crit' : warn ? 'warn' : statuses.length ? 'good' : '';

    const providers = new Set(cards.map(c => c.provider_id).filter(Boolean)).size;
    const accounts  = new Set(cards.map(c => `${c.provider_id}|${c.account_id}`).filter(s => s !== '|')).size;
    const errors    = cards.filter(c => c.error_type).length;
    const errCls    = errors > 0 ? 'crit' : '';

    aggEl.innerHTML = `
        <div class="head">
            <div class="lbl">Avg Remaining · fleet</div>
            <div class="reset">↻ ${escapeHTML(resetLbl)}</div>
        </div>
        <div class="big">${pctInt != null ? pctInt : '—'}<em>%</em></div>
        <div class="rows">
            <div class="r"><span class="l">pace</span><span class="v ${paceCls}">${paceVal}</span></div>
            <div class="r"><span class="l">errors</span><span class="v ${errCls}">${errors || '—'}</span></div>
            <div class="r"><span class="l">accounts</span><span class="v">${accounts}</span></div>
            <div class="r"><span class="l">providers</span><span class="v">${providers}</span></div>
        </div>`;
}

/** Fetch 24h history for the hero sparklines.
 *  Returns Map<forecastKey, points[]>. Failure / empty results map to absent entries.
 *  Mirrors renderConstrainedHero's selection: top 1 by pct_used, plus top 2 only when
 *  the primary is critical. */
async function _fetchCritHistory(fleet, forecastMap) {
    const eligible = (fleet || []).filter(e => {
        const c = e.critical_gauge;
        if (!c) return false;
        if (c.health === 'unknown' || c.health === 'unlimited') return false;
        if (c.error_type) return false;
        return _pctUsedOf(c) != null;
    });
    if (!eligible.length) return new Map();

    const sorted = [...eligible].sort((a, b) => {
        const ap = _pctUsedOf(a.critical_gauge) ?? 0;
        const bp = _pctUsedOf(b.critical_gauge) ?? 0;
        if (bp !== ap) return bp - ap;
        const aHit = forecastMap?.get(_forecastKeyForCard(a.critical_gauge))?.projected_limit_hit_at;
        const bHit = forecastMap?.get(_forecastKeyForCard(b.critical_gauge))?.projected_limit_hit_at;
        if (aHit && bHit) return new Date(aHit) - new Date(bHit);
        if (aHit) return -1;
        if (bHit) return 1;
        return 0;
    });

    const primaryIsCrit = sorted[0].critical_gauge.health === 'critical';
    const targets = primaryIsCrit ? sorted.slice(0, 2) : sorted.slice(0, 1);

    const results = await Promise.allSettled(targets.map(async entry => {
        const c  = entry.critical_gauge;
        const fe = forecastMap?.get(_forecastKeyForCard(c));
        // Need window_start + reset_at from the forecast to bound the request.
        // Without them we can't scope the sparkline to the current window — skip.
        if (!fe?.window_start || !fe?.reset_at) {
            return { key: _forecastKeyForCard(c), points: [] };
        }
        const data = await fetchHistoryWindowDetail({
            provider_id:  c.provider_id,
            account_id:   c.account_id,
            window_type:  c.window_type,
            window_start: fe.window_start,
            window_end:   fe.reset_at,
        });
        return { key: _forecastKeyForCard(c), points: data?.fill_series || [] };
    }));

    const map = new Map();
    for (const r of results) {
        if (r.status === 'fulfilled' && r.value.points.length) {
            map.set(r.value.key, r.value.points);
        }
    }
    return map;
}

function dimensionDisplayLabel(dim) {
    const map = {
        provider_id: 'Provider',
        account_label: 'Account',
        window_type: 'Window',
        sidecar_id: 'Sidecar'
    };
    return map[dim] || dim;
}

/** Render dimension chips and secondary chips + search in the filterbar. */
function renderFilterBar(cards) {
    const dimChipsEl = document.getElementById('dimension-chips');
    const valChipsEl = document.getElementById('provider-chips');
    if (!dimChipsEl || !valChipsEl) return;

    // 1. Render Dimension Chips
    const dimensions = ['provider_id', 'account_label', 'window_type'];
    dimChipsEl.innerHTML = dimensions.map(dim => {
        const cls = STATE.filterDimension === dim ? ' on' : '';
        const label = dimensionDisplayLabel(dim);
        return `<button class="chip${cls}" data-dim="${dim}">${escapeHTML(label)}</button>`;
    }).join('');

    // 2. Render Value Chips for active dimension
    const dim = STATE.filterDimension || 'provider_id';
    const counts = new Map();
    cards.forEach(c => {
        const k = c[dim] || '__other__';
        counts.set(k, (counts.get(k) || 0) + 1);
    });

    const activeVal = STATE.activeFilter?.dimension === dim ? STATE.activeFilter.value : null;

    const allCls = !activeVal ? ' on' : '';
    let html = `<button class="chip${allCls}" data-prov="">All<span class="n">${cards.length}</span></button>`;
    for (const [val, cnt] of [...counts.entries()].sort()) {
        const cls = activeVal === val ? ' on' : '';
        let label = val;
        if (dim === 'provider_id') label = providerDisplayLabel(val);
        html += `<button class="chip${cls}" data-prov="${escapeHTML(val)}">${escapeHTML(label)}<span class="n">${cnt}</span></button>`;
    }

    valChipsEl.innerHTML = html;
}

/** Build and inject the Fleet Commander grid (one card per provider+account).
 *
 * Driven by STATE.fleet (from /api/v1/usage/fleet). Filters from the chip bar
 * still operate on flat cards (STATE.data) — we apply the same filter to the
 * fleet entries by checking each entry's critical_gauge + secondary_limits.
 */
function renderProviderSections(cards) {
    const container = document.getElementById('dashboard-sections');
    if (!container) return;

    const fleet = STATE.fleet || [];
    if (!fleet.length) {
        // Fallback: if /fleet returned nothing (e.g. bootstrap on a fresh DB),
        // show the legacy flat-card grid so the dashboard isn't empty.
        if (cards.length) return _renderLegacyFlatGrid(cards);
        container.innerHTML = `<div class="dash-empty">NO DATA</div>`;
        return;
    }

    // Apply current filter chip to fleet entries: keep an entry if any of its
    // cards (critical_gauge + secondary_limits) match the filter.
    const filter = STATE.activeFilter;
    const matchEntry = (entry) => {
        if (!filter) return true;
        const all = [entry.critical_gauge, ...(entry.secondary_limits || [])].filter(Boolean);
        return all.some(c => c[filter.dimension] === filter.value);
    };

    let visible = fleet.filter(matchEntry);
    if (!visible.length) {
        container.innerHTML = `<div class="dash-empty">NO MATCH · <button class="toggle-btn" onclick="setFilter(null)">CLEAR FILTER</button></div>`;
        return;
    }

    // Sort entries by worst health on the critical gauge
    const SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };
    visible = [...visible].sort((a, b) => {
        const aW = SEVERITY[a.critical_gauge?.health] || 0;
        const bW = SEVERITY[b.critical_gauge?.health] || 0;
        if (aW !== bW) return bW - aW;
        return (a.provider_id || '').localeCompare(b.provider_id || '');
    });

    // Honor the saved provider order (Fleet Commander cards still group by provider)
    const providerOrder = STATE.layout?.provider_order ?? [];
    if (providerOrder.length) {
        visible = applyOrder(visible, e => e.provider_id, providerOrder);
    }

    let html = '';
    let totalCount = 0;
    for (const entry of visible) {
        try {
            html += buildFleetCommanderCard(entry, STATE.forecastMap, STATE.cumulativeMap);
            totalCount += 1 + (entry.secondary_limits || []).length;
        } catch (e) {
            console.error('buildFleetCommanderCard failed:', e, entry);
        }
    }

    container.innerHTML = html
        ? `<div class="fleet-stack">${html}</div>`
        : `<div class="dash-empty">NO MATCH</div>`;

    _wireFleetCommanderInteractions(container);

    const footerCount = document.getElementById('footer-count');
    if (footerCount) footerCount.textContent = totalCount;
}

/** Click on the wingmen toggle pill expands the .fc card to show pods. */
function _wireFleetCommanderInteractions(root) {
    root.querySelectorAll('.fc').forEach(card => {
        card.querySelectorAll('[data-toggle="pods"]').forEach(el => {
            el.addEventListener('click', e => {
                e.stopPropagation();
                card.classList.toggle('expanded');
                el.textContent = card.classList.contains('expanded') ? '▴ wingmen' : '▾ wingmen';
            });
        });
    });
}

/** Bootstrap fallback: render flat cards when /fleet has no data yet. */
function _renderLegacyFlatGrid(cards) {
    const container = document.getElementById('dashboard-sections');
    if (!container) return;

    let cardsHtml = '';
    for (const card of cards) {
        const fKey = _forecastSeriesKey(card);
        const fe = STATE.forecastMap.get(fKey);
        try { cardsHtml += buildHorizonCard(card, fe); } catch (e) { console.error('buildHorizonCard failed:', e); }
    }
    container.innerHTML = cardsHtml
        ? `<div class="section"><div class="hz-grid">${cardsHtml}</div></div>`
        : `<div class="dash-empty">NO DATA</div>`;
}

/** Open the per-card detail modal. */
export async function openCardModal(card) {
    const container = document.getElementById('modal-container');
    const content   = document.getElementById('modal-content');
    if (!container || !content) return;

    const fKey = _forecastSeriesKey(card);
    const fe   = STATE.forecastMap.get(fKey);

    // Show skeleton while fetching history
    content.innerHTML = `<div style="padding:3rem;text-align:center;"><div style="font-size:10px;color:var(--text-dim);letter-spacing:0.12em;text-transform:uppercase;">Loading…</div></div>`;
    container.classList.add('active');
    document.body.style.overflow = 'hidden';

    const closeModal = () => {
        container.classList.remove('active');
        document.body.style.overflow = '';
    };
    document.getElementById('modal-backdrop').onclick = closeModal;
    window._currentCloseModal = closeModal;

    let history24h = [];
    try {
        const raw = await fetchHistoryRaw({
            provider_id: card.provider_id,
            account_id:  card.account_id,
            days: 1,
            limit: 288,
        });
        history24h = raw.filter(p =>
            p.window_type === card.window_type &&
            (!card.service_name || p.service_name === card.service_name)
        );
    } catch (e) {
        console.warn('Could not fetch 24h history for modal:', e.message);
    }

    if (!container.classList.contains('active')) return;

    content.innerHTML = buildCardModalContent(card, fe, history24h);
    document.getElementById('close-modal').onclick = closeModal;
}

/** Full data load: limits + forecast in parallel, then render everything. */
export async function loadDashboard() {
    const myGeneration = ++loadDataGeneration;

    const sections = document.getElementById('dashboard-sections');
    const loading  = document.getElementById('loading');
    const errorBanner = document.getElementById('error-banner');

    if (sections) sections.innerHTML = '';
    if (loading)  { loading.classList.remove('hidden'); loading.style.display = 'grid'; }
    if (errorBanner) errorBanner.classList.add('hidden');

    try {
        const [limitsResult, forecastResult, fleetResult, cumulativeResult] = await Promise.allSettled([
            fetchLimits(),
            fetchForecast(),
            fetchUsageFleet(),
            fetchCumulative(),
        ]);

        if (myGeneration !== loadDataGeneration) return;

        if (limitsResult.status === 'rejected') throw limitsResult.reason;
        STATE.data = limitsResult.value.limits;

        if (forecastResult.status === 'fulfilled') {
            const newMap = new Map();
            for (const entry of (forecastResult.value.forecasts || [])) {
                newMap.set(_forecastSeriesKey(entry), entry);
            }
            STATE.forecastMap = newMap;
            STATE.forecastGeneratedAt = forecastResult.value.generated_at || null;
        }

        // STATE.fleet drives the new Fleet Commander grid; STATE.data still
        // backs the top LED strip, hero ring, and filter chips.
        STATE.fleet = fleetResult.status === 'fulfilled' ? (fleetResult.value.fleet || []) : [];

        // Cumulative totals (this period / yearly / lifetime) are keyed by
        // (provider_id, account_id) and feed the right rail of the Fleet Commander.
        STATE.cumulativeMap = new Map();
        if (cumulativeResult.status === 'fulfilled') {
            for (const entry of (cumulativeResult.value.cumulative || [])) {
                STATE.cumulativeMap.set(
                    `${entry.provider_id}|${entry.account_id || ''}`,
                    entry,
                );
            }
        }

        renderFleetHealth(STATE.data);
        renderAggregateHero(STATE.data, STATE.forecastMap);
        // First paint of the crit hero with no sparklines — fills the slots
        // immediately. Sparklines re-render once 24h history arrives below.
        renderConstrainedHero(STATE.fleet || [], STATE.forecastMap, new Map());
        renderFilterBar(STATE.data);
        renderProviderSections(STATE.data);
        window._lastFetchTime = Date.now();

        // Fire-and-forget: fetch 24h history for the top 1-2 crits, then re-render
        // those cards with sparklines. Guarded by generation so a stale fetch
        // can't overwrite a newer load's render.
        _fetchCritHistory(STATE.fleet || [], STATE.forecastMap).then(historyByKey => {
            if (myGeneration !== loadDataGeneration) return;
            if (!historyByKey.size) return;
            renderConstrainedHero(STATE.fleet || [], STATE.forecastMap, historyByKey);
        }).catch(err => console.warn('Hero sparkline history fetch failed:', err?.message));
    } catch (err) {
        if (myGeneration !== loadDataGeneration) return;
        console.error('Failed to fetch limits:', err);
        if (errorBanner) {
            errorBanner.textContent = `⚠ ${err.message || 'Unknown error occurred'}`;
            errorBanner.classList.remove('hidden');
        }
    } finally {
        if (loading) { loading.classList.add('hidden'); loading.style.display = 'none'; }
    }
}

export function setFilter(value) {
    const dim = STATE.filterDimension || 'provider_id';
    STATE.activeFilter = value ? { dimension: dim, value } : null;
    localStorage.setItem('runway_active_filter', JSON.stringify(STATE.activeFilter));
    renderFilterBar(STATE.data);
    renderProviderSections(STATE.data);
}

export function setFilterDimension(dim) {
    STATE.filterDimension = dim;
    STATE.activeFilter = null;
    localStorage.setItem('runway_filter_dimension', dim);
    localStorage.removeItem('runway_active_filter');
    renderFilterBar(STATE.data);
    renderProviderSections(STATE.data);
}

export function initDashboardView() {
    window.setFilter = setFilter;

    // Initialize the provider detail modal once
    initProviderModal();

    // Dimension chip click delegation
    const dimContainer = document.getElementById('dimension-chips');
    if (dimContainer) {
        dimContainer.addEventListener('click', e => {
            const btn = e.target.closest('button[data-dim]');
            if (!btn) return;
            setFilterDimension(btn.dataset.dim);
        });
    }

    // Provider chip click delegation
    const chipsContainer = document.getElementById('provider-chips');
    if (chipsContainer) {
        chipsContainer.addEventListener('click', e => {
            const btn = e.target.closest('button[data-prov]');
            if (!btn) return;
            const val = btn.dataset.prov;
            setFilter(val || null);
        });
    }

    // Search input
    const searchInput = document.getElementById('card-search');
    if (searchInput) {
        searchInput.addEventListener('input', () => {
            _searchQuery = searchInput.value.trim();
            renderProviderSections(STATE.data);
        });
    }

    // Fleet Commander card click → open provider modal
    const sections = document.getElementById('dashboard-sections');
    if (sections) {
        sections.addEventListener('click', e => {
            if (STATE.editMode) return;
            // Check if we clicked a Fleet Commander card (.fc)
            const fcCard = e.target.closest('article.fc');
            if (fcCard) {
                // Ignore clicks on the pods toggle
                if (e.target.closest('[data-toggle="pods"]')) return;
                const prov = fcCard.dataset.prov;
                const acc  = fcCard.dataset.acc;
                const entry = (STATE.fleet || []).find(en =>
                    (en.provider_id || '') === prov &&
                    (en.account_id  || '') === (acc || '')
                );
                if (entry) {
                    openProviderModal(entry);
                    return;
                }
            }
            // Fallback: legacy flat cards (.card but not .fc) → old per-card modal
            const card = e.target.closest('article.card:not(.fc)');
            if (!card) return;
            const cardKey_ = card.dataset.cardKey;
            const prov = card.dataset.prov;
            const found = STATE.data.find(c =>
                (c.provider_id || '') === prov && cardKey(c) === cardKey_
            );
            if (found) openCardModal(found);
        });
    }
}

// Cross-view navigation: close modal → switch to history view with provider filter
window.openProviderInHistory = function(providerId) {
    const container = document.getElementById('modal-container');
    if (container) {
        container.classList.remove('active');
        document.body.style.overflow = '';
    }
    STATE.activeFilter = { dimension: 'provider_id', value: providerId };
    STATE.filterDimension = 'provider_id';
    localStorage.setItem('runway_active_filter', JSON.stringify(STATE.activeFilter));
    localStorage.setItem('runway_filter_dimension', 'provider_id');
    if (typeof window.switchView === 'function') window.switchView('history');
};
