// Dashboard view module - lazy loaded via dynamic import
import { fetchLimits, fetchForecast, fetchHistoryRaw, fetchUsageFleet } from '../api.js';
import { STATE } from '../state.js';
import { buildHorizonCard, buildCardModalContent, providerDisplayLabel, buildFleetCommanderCard } from '../components.js';
import { cardKey, applyOrder } from '../layout.js';

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
}

/** Render the aggregate % remaining ring + hero numbers. */
function renderAggregateHero(cards, forecastMap) {
    const ringEl     = document.getElementById('agg-ring');
    const pctValEl   = document.getElementById('agg-pct-val');
    const resetLineEl = document.getElementById('agg-reset-line');
    const paceLineEl  = document.getElementById('agg-pace-line');

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

    // Aggregate pace from worst forecast status
    if (paceLineEl) {
        const statuses = [...forecastMap.values()].map(f => f.status).filter(Boolean);
        const fast = statuses.some(s => s === 'risk' || s === 'exhausted');
        const warn = !fast && statuses.some(s => s === 'warn');
        if (fast)       paceLineEl.innerHTML = `<span class="pace-dot fast" style="display:inline-block;"></span>pace fast`;
        else if (warn)  paceLineEl.innerHTML = `<span class="pace-dot moderate" style="display:inline-block;"></span>pace moderate`;
        else if (statuses.some(s => s === 'ok' || s === 'stable'))
                        paceLineEl.innerHTML = `<span class="pace-dot" style="display:inline-block;"></span>pace stable`;
        else            paceLineEl.textContent = '';
    }

    // SVG ring
    if (!ringEl) return;
    if (pctInt == null) { ringEl.innerHTML = ''; return; }

    const r = 75, C = 2 * Math.PI * r;
    const offset = C * (1 - pctInt / 100);
    ringEl.innerHTML = `
        <svg viewBox="0 0 170 170">
            <circle class="track" cx="85" cy="85" r="${r}"/>
            <circle class="progress" cx="85" cy="85" r="${r}"
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
            html += buildFleetCommanderCard(entry, STATE.forecastMap);
            totalCount += 1 + (entry.secondary_limits || []).length;
        } catch (e) {
            console.error('buildFleetCommanderCard failed:', e, entry);
        }
    }

    container.innerHTML = html
        ? `<div class="hz-grid fleet-grid">${html}</div>`
        : `<div class="dash-empty">NO MATCH</div>`;

    _wireFuelDumpToggles(container);

    const footerCount = document.getElementById('footer-count');
    if (footerCount) footerCount.textContent = totalCount;
}

/** Click on a Fuel Dump bar toggles the matching Wingman Pods row open/closed. */
function _wireFuelDumpToggles(root) {
    root.querySelectorAll('.fleet-commander').forEach(commander => {
        const bar = commander.querySelector('.fuel-dump-bar');
        const row = commander.querySelector('.wingman-row');
        if (!bar || !row) return;
        bar.addEventListener('click', () => {
            const expanded = bar.getAttribute('aria-expanded') === 'true';
            bar.setAttribute('aria-expanded', String(!expanded));
            if (expanded) row.setAttribute('hidden', '');
            else row.removeAttribute('hidden');
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
        const [limitsResult, forecastResult, fleetResult] = await Promise.allSettled([
            fetchLimits(),
            fetchForecast(),
            fetchUsageFleet(),
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

        renderFleetHealth(STATE.data);
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

    // Card click delegation (open per-card modal)
    const sections = document.getElementById('dashboard-sections');
    if (sections) {
        sections.addEventListener('click', e => {
            const card = e.target.closest('article.card');
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
