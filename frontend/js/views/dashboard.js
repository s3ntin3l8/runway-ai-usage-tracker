// Dashboard view module - lazy loaded via dynamic import
import { fetchLimits, fetchForecast, fetchHistoryRaw, fetchUsageFleet, fetchCumulative } from '../api.js';
import { STATE } from '../state.js';
import { buildHorizonCard, buildCardModalContent, providerDisplayLabel, buildFleetCommanderCard } from '../components.js';
import { cardKey, applyOrder } from '../layout.js';
import { openProviderModal, initProviderModal } from './modal/index.js';

let loadDataGeneration = 0;
let _searchQuery = '';

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

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

/** Render fleet-health lights — one cell per card, no padding. */
function renderFleetHealth(cards) {
    const lightsEl  = document.getElementById('fleet-lights');
    const nominalEl = document.getElementById('fleet-nominal');
    const totalEl   = document.getElementById('fleet-total');
    const countsEl  = document.getElementById('fleet-counts');
    if (!lightsEl) return;

    const buckets = cards.map(c => {
        if (c.health === 'critical') return 'crit';
        if (c.health === 'warning')  return 'warn';
        if (c.health === 'good' || c.health === 'unlimited') return '';
        return 'off';
    });

    // Dynamic column count: repeat(N, 1fr)
    lightsEl.style.gridTemplateColumns = `repeat(${Math.max(1, buckets.length)}, 1fr)`;
    lightsEl.innerHTML = cards.map((c, i) => {
        const cls = buckets[i];
        const h = c.health || 'unknown';
        const status = h.charAt(0).toUpperCase() + h.slice(1);
        const pid = c.provider_id || '??';
        return `
            <div class="tooltip-container" style="height:100%;">
                <i class="${cls}" style="height:100%;width:100%;"></i>
                <div class="tooltip" style="bottom:100%;margin-bottom:8px;z-index:300;transform:translateX(50%);right:50%;">
                    <div style="font-weight:700;margin-bottom:2px;">${escapeHTML(pid.toUpperCase())}</div>
                    <div style="font-size:10px;color:var(--text-dim);">${escapeHTML(c.service_name)} · ${escapeHTML(status)}</div>
                </div>
            </div>`;
    }).join('');

    const nominal = cards.filter(c => c.health === 'good' || c.health === 'unlimited').length;
    if (nominalEl) nominalEl.textContent = nominal;
    if (totalEl)   totalEl.textContent   = cards.length;

    // Compute subtext: sidecars · providers · accounts
    if (countsEl) {
        const sidecars  = new Set(cards.map(c => c.sidecar_id).filter(Boolean)).size;
        const providers = new Set(cards.map(c => c.provider_id).filter(Boolean)).size;
        const accounts  = new Set(cards.map(c => `${c.provider_id}|${c.account_id}`).filter(c => c !== '|')).size;
        const parts = [];
        if (sidecars > 0) parts.push(`${sidecars} sidecar${sidecars !== 1 ? 's' : ''}`);
        parts.push(`${providers} provider${providers !== 1 ? 's' : ''}`);
        parts.push(`${accounts} account${accounts !== 1 ? 's' : ''}`);
        countsEl.textContent = parts.join(' · ');
    }

    // Bucket counts row
    const nOk   = cards.filter(c => c.health === 'good' || c.health === 'unlimited').length;
    const nWarn = cards.filter(c => c.health === 'warning').length;
    const nCrit = cards.filter(c => c.health === 'critical').length;
    const nErr  = cards.filter(c => c.error_type).length;
    const bucketsEl = document.getElementById('health-buckets');
    if (bucketsEl) {
        bucketsEl.innerHTML = [
            `<div class="bk"><span class="bk-num">${nOk}</span><span>OK</span></div>`,
            nWarn ? `<div class="bk"><span class="bk-num warn">${nWarn}</span><span>WARN</span></div>` : '',
            nCrit ? `<div class="bk"><span class="bk-num crit">${nCrit}</span><span>CRIT</span></div>` : '',
            nErr  ? `<div class="bk"><span class="bk-num err">${nErr}</span><span>ERR</span></div>`  : '',
        ].join('');
    }

    // Per-provider mini-table
    const provTableEl = document.getElementById('prov-table');
    if (provTableEl) {
        const byProv = new Map();
        cards.forEach(c => {
            if (!c.provider_id) return;
            const g = byProv.get(c.provider_id) || { cards: [] };
            g.cards.push(c);
            byProv.set(c.provider_id, g);
        });
        const SEVER = { critical: 4, warning: 3, good: 2, unlimited: 1, unknown: 0 };
        const rows = [...byProv.entries()].map(([pid, g]) => {
            const worst = g.cards.reduce((a, b) => (SEVER[b.health] || 0) > (SEVER[a.health] || 0) ? b : a);
            const pcts  = g.cards.map(c => c.pct_used != null ? c.pct_used
                : (c.used_value && c.limit_value ? c.used_value / c.limit_value * 100 : null))
                .filter(p => p != null);
            const avgUsed   = pcts.length ? pcts.reduce((a, b) => a + b, 0) / pcts.length : null;
            const remaining = avgUsed != null ? Math.round(100 - avgUsed) : null;
            const hCls      = worst.health === 'critical' ? 'crit' : worst.health === 'warning' ? 'warn' : 'good';
            const label     = providerDisplayLabel(pid);
            const barPct    = remaining != null ? Math.max(0, Math.min(100, remaining)) : 0;
            return `<div class="prov-row">
                <span style="font-size:11px;color:var(--text-dim);">${escapeHTML(label.charAt(0).toUpperCase())}</span>
                <div>
                    <div style="font-size:9px;color:var(--text);letter-spacing:0.04em;margin-bottom:2px;">${escapeHTML(label)}</div>
                    <div class="prov-bar-wrap"><div class="prov-bar-fill ${hCls}" style="width:${barPct}%"></div></div>
                </div>
                <span class="prov-pct">${remaining != null ? remaining + '%' : '—'}</span>
                <span class="prov-status ${hCls}">${hCls.toUpperCase()}</span>
            </div>`;
        });
        provTableEl.innerHTML = rows.join('');
    }
}

/** Render the Most Constrained centre hero panel. */
function renderMostConstrained(fleet) {
    const el = document.getElementById('most-constrained-content');
    if (!el) return;
    const header = `<div class="mc-head">Most Constrained</div>`;
    const SEVER = { critical: 4, warning: 3, good: 2, unlimited: 1, unknown: 0 };

    const atRisk = (fleet || []).filter(e => e.critical_gauge &&
        (e.critical_gauge.health === 'critical' || e.critical_gauge.health === 'warning'));

    if (!atRisk.length) {
        el.innerHTML = `<div class="mc-panel">${header}<div class="mc-clear">
            <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            <div class="mc-clear-label">All systems nominal</div>
        </div></div>`;
        return;
    }

    const worst = [...atRisk].sort((a, b) => {
        const hd = (SEVER[b.critical_gauge.health] || 0) - (SEVER[a.critical_gauge.health] || 0);
        if (hd !== 0) return hd;
        return (b.critical_gauge.pct_used || 0) - (a.critical_gauge.pct_used || 0);
    })[0];

    const c = worst.critical_gauge;
    const pct_used  = c.pct_used ?? (c.used_value && c.limit_value ? c.used_value / c.limit_value * 100 : null);
    const remaining = pct_used != null ? Math.round(100 - pct_used) : null;
    const hCls      = c.health === 'critical' ? 'crit' : 'warn';

    const fKey = [c.provider_id || '', c.account_id || '', c.service_name || '', c.variant || '', c.model_id || '', c.window_type || '', c.unit_type || ''].join('||');
    const fe   = STATE.forecastMap?.get(fKey);
    const paceLabel = fe?.status === 'risk' || fe?.status === 'exhausted' ? 'FAST ⚠'
        : fe?.status === 'warn' ? 'MODERATE' : fe?.status === 'ok' ? 'STABLE' : '—';
    const landAt = fe?.exhaustion_at ? (() => {
        const h = Math.floor((new Date(fe.exhaustion_at) - Date.now()) / 3600000);
        return h > 0 ? `${h}h` : h === 0 ? '<1h' : 'DONE';
    })() : '—';
    const tokens     = c.token_usage?.total ? (c.token_usage.total / 1e6).toFixed(1) + 'M' : '—';
    const windowLabel = c.window_type ? c.window_type.replace(/_/g, ' ') : '—';

    el.innerHTML = `<div class="mc-panel">${header}
        <div class="mc-provider">${escapeHTML(providerDisplayLabel(c.provider_id || ''))} · ${escapeHTML(c.account_label || c.account_id || '')}</div>
        <div class="mc-service">${escapeHTML(c.service_name || c.window_type || '')}</div>
        <div class="mc-pct ${hCls}">${remaining != null ? remaining : '—'}<em>%</em></div>
        <div class="mc-bar-wrap"><div class="mc-bar-fill ${hCls}" style="width:${Math.max(0, Math.min(100, remaining ?? 0))}%"></div></div>
        <div class="mc-stats">
            <div class="mc-stat"><div class="mc-stat-label">Pace</div><div class="mc-stat-val">${escapeHTML(paceLabel)}</div></div>
            <div class="mc-stat"><div class="mc-stat-label">Land at</div><div class="mc-stat-val">${escapeHTML(landAt)}</div></div>
            <div class="mc-stat"><div class="mc-stat-label">Tokens</div><div class="mc-stat-val">${escapeHTML(tokens)}</div></div>
            <div class="mc-stat"><div class="mc-stat-label">Window</div><div class="mc-stat-val">${escapeHTML(windowLabel)}</div></div>
        </div>
    </div>`;
}

/** Render the aggregate % remaining ring + hero numbers. */
function renderAggregateHero(cards, forecastMap) {
    const ringEl      = document.getElementById('agg-ring');
    const pctValEl    = document.getElementById('agg-pct-val');
    const resetLineEl = document.getElementById('agg-reset-line');

    // Only include cards that have a computable remaining %
    const eligible = cards.filter(c =>
        !c.error_type && !c.is_unlimited && c.health !== 'unknown' &&
        c.pct_used != null || (c.used_value != null && c.limit_value)
    );

    const pctOf = c => c.pct_used != null ? c.pct_used
        : (c.used_value != null && c.limit_value ? c.used_value / c.limit_value * 100 : null);

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

    if (pctValEl) pctValEl.textContent = pctInt != null ? pctInt : '—';

    // Soonest reset
    const resets = cards.map(c => c.reset_at).filter(Boolean).map(r => new Date(r)).filter(d => !isNaN(d));
    if (resetLineEl) {
        if (resets.length) {
            const soonest = new Date(Math.min(...resets));
            const diffMs  = soonest - Date.now();
            const h = Math.floor(diffMs / 3600000);
            const m = Math.floor((diffMs % 3600000) / 60000);
            const label = diffMs < 0 ? 'now' : h > 0 ? `${h}h ${m}m` : `${m}m`;
            resetLineEl.innerHTML = `resets in <b>${label}</b>`;
        } else {
            resetLineEl.textContent = '—';
        }
    }

    // Mini-stats grid (replaces pace line)
    const miniStatsEl = document.getElementById('agg-mini-stats');
    if (miniStatsEl) {
        const statuses = [...forecastMap.values()].map(f => f.status).filter(Boolean);
        const fast = statuses.some(s => s === 'risk' || s === 'exhausted');
        const warn = !fast && statuses.some(s => s === 'warn');
        const paceVal = fast ? 'FAST' : warn ? 'MODERATE' : statuses.length ? 'STABLE' : '—';
        const paceCls = fast ? 'pace-fast' : warn ? 'pace-moderate' : statuses.length ? 'pace-stable' : '';
        const providers = new Set(cards.map(c => c.provider_id).filter(Boolean)).size;
        const accounts  = new Set(cards.map(c => `${c.provider_id}|${c.account_id}`).filter(s => s !== '|')).size;
        const errors    = cards.filter(c => c.error_type).length;
        const errCls    = errors > 0 ? 'err-present' : '';
        miniStatsEl.innerHTML = [
            `<div class="agg-stat-row"><span class="asr-label">fleet pace</span><span class="asr-val ${paceCls}">${paceVal}</span></div>`,
            `<div class="agg-stat-row"><span class="asr-label">accounts</span><span class="asr-val">${accounts}</span></div>`,
            `<div class="agg-stat-row"><span class="asr-label">errors</span><span class="asr-val ${errCls}">${errors || '—'}</span></div>`,
            `<div class="agg-stat-row"><span class="asr-label">providers</span><span class="asr-val">${providers}</span></div>`,
        ].join('');
    }

    // SVG ring (compact: r=55, 130×130 viewBox)
    if (!ringEl) return;
    if (pctInt == null) { ringEl.innerHTML = ''; return; }

    const r = 55, C = 2 * Math.PI * r;
    const offset = C * (1 - pctInt / 100);
    ringEl.innerHTML = `
        <svg viewBox="0 0 130 130">
            <circle class="track" cx="65" cy="65" r="${r}"/>
            <circle class="progress" cx="65" cy="65" r="${r}"
                stroke-dasharray="${C.toFixed(2)}"
                stroke-dashoffset="${offset.toFixed(2)}"/>
        </svg>
        <div class="c">
            <div class="v">${pctInt}%</div>
            <div class="l">remain</div>
        </div>`;
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
        renderMostConstrained(STATE.fleet || []);
        renderAggregateHero(STATE.data, STATE.forecastMap);
        renderFilterBar(STATE.data);
        renderProviderSections(STATE.data);
        window._lastFetchTime = Date.now();
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
