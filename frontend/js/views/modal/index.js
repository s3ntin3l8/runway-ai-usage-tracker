/**
 * Provider detail modal — tabbed drilldown for Fleet Commander cards.
 *
 * Exports:
 *   openProviderModal(entry)  — open modal for a fleet entry
 *   closeProviderModal()      — close the modal
 *
 * The modal markup is injected into #provider-modal (added to index.html).
 */

import { fetchHeatmap, fetchSessions } from '../../api.js';
import { getUserTz } from '../../utils/tz.js';
import { STATE } from '../../state.js';
import { providerDisplayLabel } from '../../components.js';
import { escapeHTML as _esc } from '../../utils/html.js';
import { buildOverviewPane, wireOverviewSparkTabs } from './overview.js';
import { buildUsagePane, wireUsageSparkTabs } from './usage.js';
import { buildCostPane } from './cost.js';
import { buildDebugPane } from './debug.js';

// Cached data per open modal session, cleared on each open
let _modalCache = {};

// Currently active tab name
let _activeTab = 'overview';

// The fleet entry currently displayed
let _currentEntry = null;

// Cached token health (fetched once per session). undefined = not yet fetched.
let _tokenHealthCache = undefined;

/**
 * Fetch /api/v1/system/token-health.
 * Returns null gracefully if the endpoint doesn't exist or fails.
 */
async function _fetchTokenHealth() {
    if (_tokenHealthCache !== undefined) return _tokenHealthCache;
    try {
        const adminKey = localStorage.getItem('runway_admin_key');
        const headers = adminKey ? { 'X-Admin-Key': adminKey } : {};
        const resp = await fetch('/api/v1/system/token-health', { headers });
        if (!resp.ok) {
            _tokenHealthCache = null;
            return null;
        }
        _tokenHealthCache = await resp.json();
        return _tokenHealthCache;
    } catch {
        _tokenHealthCache = null;
        return null;
    }
}

/**
 * Render the current pane into #pm-body.
 * Fetches data on first render of each tab; uses cache afterwards.
 */
async function _renderPane(tab) {
    const body = document.getElementById('pm-body');
    if (!body || !_currentEntry) return;

    const entry      = _currentEntry;
    const providerId = entry.provider_id || '';
    const accountId  = entry.account_id || '';
    const cumData    = STATE.cumulativeMap?.get?.(`${providerId}|${accountId}`) || null;

    // Show loading indicator while fetching
    body.innerHTML = '<div class="pm-loading">Loading…</div>';

    try {
        if (tab === 'overview') {
            if (!_modalCache.heatmap) {
                try {
                    const hd = await fetchHeatmap({ provider_id: providerId, account_id: accountId, days: 14, tz: getUserTz() });
                    _modalCache.heatmap = hd.cells || [];
                } catch { _modalCache.heatmap = []; }
            }
            if (!_modalCache.recentSessions) {
                try {
                    const sd = await fetchSessions({ provider_id: providerId, account_id: accountId, limit: 3, sort_by: 'recent' });
                    _modalCache.recentSessions = sd.sessions || [];
                } catch { _modalCache.recentSessions = []; }
            }
            body.innerHTML = buildOverviewPane(entry, cumData, _modalCache.heatmap, _modalCache.recentSessions);
            wireOverviewSparkTabs(_modalCache.heatmap);

        } else if (tab === 'usage') {
            if (!_modalCache.heatmap) {
                try {
                    const hd = await fetchHeatmap({ provider_id: providerId, account_id: accountId, days: 14, tz: getUserTz() });
                    _modalCache.heatmap = hd.cells || [];
                } catch { _modalCache.heatmap = []; }
            }
            if (!_modalCache.sessions) {
                try {
                    const sd = await fetchSessions({ provider_id: providerId, account_id: accountId, limit: 10 });
                    _modalCache.sessions = sd.sessions || [];
                } catch { _modalCache.sessions = []; }
            }
            body.innerHTML = buildUsagePane(entry, { cells: _modalCache.heatmap }, _modalCache.sessions);
            wireUsageSparkTabs(_modalCache.heatmap);

        } else if (tab === 'cost') {
            body.innerHTML = buildCostPane(entry, cumData);

        } else if (tab === 'debug') {
            const tokenHealth = await _fetchTokenHealth();
            body.innerHTML = buildDebugPane(entry, tokenHealth);
        }
    } catch (err) {
        body.innerHTML = `<div class="pm-error">Error loading ${_esc(tab)} pane: ${_esc(err.message || String(err))}</div>`;
        console.error('[provider-modal] pane render error:', err);
    }
}

/**
 * Open the provider detail modal for a given fleet entry.
 * @param {object} entry - Fleet entry with critical_gauge, secondary_limits, sidecar_contributions
 */
export async function openProviderModal(entry) {
    const modal = document.getElementById('provider-modal');
    if (!modal) {
        console.warn('[provider-modal] #provider-modal element not found');
        return;
    }

    // Reset state
    _currentEntry = entry;
    _activeTab = 'overview';
    _modalCache = {};
    // Don't reset _tokenHealthCache — it's session-scoped

    const providerId  = entry.provider_id || '';
    const critical    = entry.critical_gauge || {};
    const provLabel   = providerDisplayLabel(providerId) || providerId;
    const accountLabel = critical.account_label || entry.account_id || 'default';
    const plan        = critical.tier || critical.plan || '';
    const windowType  = critical.window_type || '';
    const dataSource  = critical.data_source || '';
    const polledAgo   = critical.fetched_at
        ? (() => {
            const s = (Date.now() - new Date(critical.fetched_at).getTime()) / 1000;
            return s < 60 ? Math.round(s) + 's' : Math.round(s / 60) + 'm';
          })() + ' ago'
        : '—';

    // Update header
    const logoEl = document.getElementById('pm-logo');
    if (logoEl) {
        logoEl.textContent = (provLabel || '?')[0].toUpperCase();
        logoEl.className = `plogo c-${_esc(providerId)}`;
    }
    const titleEl = document.getElementById('pm-title');
    if (titleEl) {
        const suffix = plan || windowType || 'account';
        titleEl.textContent = `${provLabel} · ${suffix}`;
    }
    const subEl = document.getElementById('pm-sub');
    if (subEl) {
        subEl.textContent = `${accountLabel} · ${dataSource || 'local'} · polled ${polledAgo}`;
    }

    // Reset tabs
    document.querySelectorAll('#pm-tabs button').forEach(b => {
        b.classList.toggle('on', b.dataset.tab === 'overview');
    });

    // Show modal
    modal.removeAttribute('hidden');
    modal.classList.add('open');
    document.body.style.overflow = 'hidden';

    // Render first pane
    await _renderPane('overview');
}

/** Close the provider detail modal. */
export function closeProviderModal() {
    const modal = document.getElementById('provider-modal');
    if (!modal) return;
    modal.classList.remove('open');
    // Use a short delay before hidden to allow CSS transition
    setTimeout(() => {
        if (!modal.classList.contains('open')) {
            modal.setAttribute('hidden', '');
        }
    }, 200);
    document.body.style.overflow = '';
    _currentEntry = null;
    _modalCache = {};
}

/** Initialize modal event listeners. Call once on app startup. */
export function initProviderModal() {
    // Create modal HTML if not already in DOM
    if (!document.getElementById('provider-modal')) {
        _injectModalMarkup();
    }

    // Close button
    document.getElementById('pm-close')?.addEventListener('click', closeProviderModal);

    // Click on backdrop
    document.getElementById('provider-modal')?.addEventListener('click', e => {
        if (e.target.id === 'provider-modal') closeProviderModal();
    });

    // Escape key
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') {
            const modal = document.getElementById('provider-modal');
            if (modal && !modal.hasAttribute('hidden') && modal.classList.contains('open')) {
                closeProviderModal();
            }
        }
    });

    // Tab switching — delegate from #pm-tabs (which is inside the injected markup)
    document.addEventListener('click', async e => {
        const btn = e.target.closest('#pm-tabs button');
        if (!btn || !btn.dataset.tab) return;
        const tab = btn.dataset.tab;
        if (tab === _activeTab) return;
        _activeTab = tab;
        document.querySelectorAll('#pm-tabs button').forEach(b => {
            b.classList.toggle('on', b.dataset.tab === tab);
        });
        await _renderPane(tab);
    });
}

/** Inject modal HTML into body if not already present. */
function _injectModalMarkup() {
    const div = document.createElement('div');
    div.innerHTML = `
<div class="modal-bg" id="provider-modal" hidden>
    <div class="modal glass raised">
        <div class="hd">
            <div class="plogo" id="pm-logo">A</div>
            <div>
                <div class="title" id="pm-title">Provider · Account</div>
                <div class="sub" id="pm-sub">subtitle</div>
            </div>
            <span class="x" id="pm-close" aria-label="Close">×</span>
        </div>
        <nav class="tabs" id="pm-tabs">
            <button data-tab="overview" class="on">Overview</button>
            <button data-tab="usage">Usage</button>
            <button data-tab="cost">Cost</button>
            <button data-tab="debug">Debug</button>
        </nav>
        <div class="body" id="pm-body">
            <!-- pane content injected here -->
        </div>
    </div>
</div>`;
    document.body.appendChild(div.firstElementChild);
}
