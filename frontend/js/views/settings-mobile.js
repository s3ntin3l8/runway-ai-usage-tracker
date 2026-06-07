/**
 * iOS-style Settings drill-in (mobile, ≤640px).
 *
 * Menu → section subpage → back (and Providers → per-provider page), with
 * horizontal slide transitions. Reuses the desktop section renderers from
 * settings.js — only the navigation shell and the provider list/detail
 * chrome are mobile-specific. Desktop settings DOM is untouched; this shell
 * is injected into #view-settings and removed when leaving mobile.
 */

import { fetchProviderConfigs, putProviderConfig } from '../api.js';
import { STATE } from '../state.js';
import { escapeHTML, escapeHTMLAttr } from '../utils/html.js';
import { providerIconUrl } from '../components/_shared.js';
import { openProviderModal } from './modal/index.js';
import {
    POLL_OPTIONS,
    renderTokensSection,
    renderWebhooksSection,
    renderSystemSection,
    renderDisplaySection,
    renderAuditSection,
    buildProviderDetailHTML,
    wireProviderDetail,
} from './settings.js';

const _CHEV = '<span class="ios-chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg></span>';
const _BACK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></svg>';

const _MENU = [
    {
        label: 'Connections',
        items: [
            { id: 'providers', label: 'Providers', sub: 'Collection · poll intervals · credentials', count: 'sm-count-providers', icon: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>' },
            { id: 'tokens', label: 'Token health', sub: 'Credential expiry · refresh', count: 'sm-count-tokens', icon: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.5 12.5 8-8"/><path d="m16 7 3 3"/><path d="m19 4 2 2"/>' },
            { id: 'webhooks', label: 'Webhooks', sub: 'Slack · Discord alerts', count: 'sm-count-webhooks', icon: '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1.5-1.5"/>' },
        ],
    },
    {
        label: 'Configuration',
        items: [
            { id: 'system', label: 'System', sub: 'API key · timezone · retention', icon: '<line x1="4" y1="6" x2="20" y2="6"/><circle cx="9" cy="6" r="2"/><line x1="4" y1="12" x2="20" y2="12"/><circle cx="15" cy="12" r="2"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="11" cy="18" r="2"/>' },
            { id: 'display', label: 'Display', sub: 'Theme · density · accent', icon: '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18Z" fill="currentColor"/>' },
            { id: 'audit', label: 'Audit log', sub: 'Recent configuration events', icon: '<line x1="8" y1="6" x2="20" y2="6"/><line x1="8" y1="12" x2="20" y2="12"/><line x1="8" y1="18" x2="20" y2="18"/><line x1="3.5" y1="6" x2="3.51" y2="6"/><line x1="3.5" y1="12" x2="3.51" y2="12"/><line x1="3.5" y1="18" x2="3.51" y2="18"/>' },
        ],
    },
];

const _SECTION_TITLES = {
    providers: 'Providers', tokens: 'Token health', webhooks: 'Webhooks',
    system: 'System', display: 'Display', audit: 'Audit log',
};

let _navStack = ['menu'];
let _providersCache = null;

function _root() { return document.getElementById('settings-mobile'); }

function _showPane(id, dir) {
    const root = _root();
    if (!root) return;
    root.querySelectorAll('.ios-pane').forEach(p =>
        p.classList.remove('active', 'slide-fwd', 'slide-back'));
    const pane = root.querySelector(`.ios-pane[data-pane="${id}"]`);
    if (!pane) return;
    pane.classList.add('active', dir === 'back' ? 'slide-back' : 'slide-fwd');
    window.scrollTo({ top: 0 });
}

function _push(id) { _navStack.push(id); _showPane(id, 'fwd'); }
function _pop() {
    if (_navStack.length <= 1) return;
    _navStack.pop();
    _showPane(_navStack[_navStack.length - 1], 'back');
}

function _menuRowHTML(item) {
    const count = item.count ? `<span class="ios-count" id="${item.count}"></span>` : '<span></span>';
    return `<button class="ios-row" data-goto="${item.id}">
        <span class="ios-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${item.icon}</svg></span>
        <span class="ios-rowtext"><span class="ios-label">${escapeHTML(item.label)}</span><span class="ios-sub">${escapeHTML(item.sub)}</span></span>
        ${count}
        ${_CHEV}
    </button>`;
}

function _subheadHTML(title, backLabel) {
    return `<div class="ios-subhead">
        <button class="ios-back" type="button">${_BACK}${escapeHTML(backLabel)}</button>
        <span class="ios-title">${escapeHTML(title)}</span>
    </div>`;
}

export function renderSettingsMobile() {
    const view = document.getElementById('view-settings');
    if (!view) return;
    _navStack = ['menu'];
    _providersCache = null;

    document.getElementById('settings-mobile')?.remove();
    const root = document.createElement('div');
    root.id = 'settings-mobile';
    root.className = 'ios-stack';

    const menuGroups = _MENU.map(g => `<div class="ios-group">
        <div class="ios-group-label">${escapeHTML(g.label)}</div>
        <div class="ios-list">${g.items.map(_menuRowHTML).join('')}</div>
    </div>`).join('');

    const sectionPanes = Object.entries(_SECTION_TITLES).map(([id, title]) => `
        <section class="ios-pane" data-pane="${id}">
            ${_subheadHTML(title, 'Settings')}
            <div class="sm-pane-body" id="sm-pane-${id}"></div>
        </section>`).join('');

    root.innerHTML = `
        <section class="ios-pane active" data-pane="menu">
            <h2 class="ios-menu-title">Settings</h2>
            ${menuGroups}
        </section>
        ${sectionPanes}
        <section class="ios-pane" data-pane="provider">
            ${_subheadHTML('Provider', 'Providers')}
            <div class="sm-pane-body ios-detail-body" id="sm-provider-detail"></div>
        </section>`;
    view.appendChild(root);

    // Navigation wiring (delegated once per shell build)
    root.addEventListener('click', (e) => {
        const back = e.target.closest('.ios-back');
        if (back) { _pop(); return; }
        const goto = e.target.closest('[data-goto]');
        if (goto) { _openSection(goto.dataset.goto); return; }
        const provRow = e.target.closest('[data-sm-provider]');
        if (provRow) { _openProviderDetail(provRow.dataset.smProvider); return; }
    });

    _fillMenuCounts();
}

async function _fillMenuCounts() {
    // Reuses the same endpoints as the desktop sidebar counts; fire-and-forget.
    fetchProviderConfigs()
        .then(({ providers }) => {
            _providersCache = providers;
            const el = document.getElementById('sm-count-providers');
            if (el) el.textContent = providers.length;
        })
        .catch(() => {});
    import('../api.js').then(api => {
        api.fetchTokenHealth()
            .then(h => {
                const el = document.getElementById('sm-count-tokens');
                if (el) el.textContent = (h.tokens || []).length;
            })
            .catch(() => {});
        api.fetchWithAuth('/api/v1/system/webhooks')
            .then(r => r.json())
            .then(d => {
                const el = document.getElementById('sm-count-webhooks');
                if (el) el.textContent = (d.webhooks || []).length;
            })
            .catch(() => {});
    });
}

function _openSection(id) {
    const body = document.getElementById(`sm-pane-${id}`);
    if (!body) return;
    _push(id);
    body.innerHTML = '<p class="sm-loading">Loading…</p>';
    if (id === 'providers') { _renderProviderList(body); return; }
    const renderers = {
        tokens: renderTokensSection,
        webhooks: renderWebhooksSection,
        system: renderSystemSection,
        display: renderDisplaySection,
        audit: renderAuditSection,
    };
    renderers[id]?.(body);
}

// ─── Providers list (level 1) ────────────────────────────────────────────────

function _providerLogoHTML(p, size = 30) {
    const iconUrl = providerIconUrl(p.provider_id);
    const initial = escapeHTML(p.icon || (p.name?.[0] ?? '?'));
    return `<div class="plogo c-${escapeHTMLAttr(p.provider_id)}${iconUrl ? ' has-icon' : ''}" style="width:${size}px;height:${size}px">${
        iconUrl
            ? `<img class="plogo-img" src="${escapeHTMLAttr(iconUrl)}" alt="" loading="lazy" onerror="const x=this.parentElement;x.classList.remove('has-icon');x.innerHTML='${initial}'">`
            : initial
    }</div>`;
}

async function _renderProviderList(body) {
    try {
        const { providers } = await fetchProviderConfigs();
        _providersCache = providers;
        if (!providers.length) {
            body.innerHTML = '<p class="sm-loading">No providers configured.</p>';
            return;
        }
        body.innerHTML = `<div class="ios-group">
            <div class="ios-group-label">Connected · ${providers.length}</div>
            <div class="ios-list">${providers.map(p => `
                <button class="ios-row" data-sm-provider="${escapeHTMLAttr(p.provider_id)}">
                    ${_providerLogoHTML(p)}
                    <span class="ios-rowtext">
                        <span class="ios-label">${escapeHTML(p.name)}</span>
                        <span class="ios-sub">${escapeHTML(p.account_label || 'auto-detected account')}</span>
                    </span>
                    <span class="ios-val${p.enabled ? ' on' : ''}">${p.enabled ? 'On' : 'Off'}</span>
                    ${_CHEV}
                </button>`).join('')}
            </div>
        </div>`;
    } catch (err) {
        body.innerHTML = `<p class="sm-loading" style="color:var(--crit);">Failed to load providers: ${escapeHTML(err.message)}</p>`;
    }
}

// ─── Per-provider detail (level 2) ───────────────────────────────────────────

async function _openProviderDetail(providerId, { refresh = false } = {}) {
    if (refresh || !_providersCache) {
        try { _providersCache = (await fetchProviderConfigs()).providers; } catch { /* keep stale */ }
    }
    const p = (_providersCache || []).find(x => x.provider_id === providerId);
    if (!p) return;

    const pane = _root()?.querySelector('.ios-pane[data-pane="provider"]');
    const body = document.getElementById('sm-provider-detail');
    if (!pane || !body) return;
    pane.querySelector('.ios-title').textContent = p.name;

    const pollOpts = POLL_OPTIONS.map(o =>
        `<option value="${o.value}" ${p.poll_interval_seconds === o.value ? 'selected' : ''}>${o.label}</option>`).join('');

    // A live fleet entry means the dashboard has a card for this provider —
    // surface the same detail modal the cards open.
    const fleetEntry = (STATE.fleet || []).find(en => (en.provider_id || '') === providerId);
    const liveBtn = fleetEntry
        ? '<button class="sm-live-btn" id="sm-live-usage" type="button">View live usage →</button>'
        : '';

    body.innerHTML = `
        <div class="ios-detail-hero">
            ${_providerLogoHTML(p, 46)}
            <div>
                <div class="ios-detail-name">${escapeHTML(p.name)}</div>
                <div class="ios-detail-meta">${escapeHTML(p.account_label || 'auto-detected')} · polls ${escapeHTML(String(p.effective_poll_interval))}s</div>
            </div>
        </div>
        <div class="ios-group">
            <div class="ios-group-label">Collection</div>
            <div class="ios-card" data-drill-enabled>
                <div class="pd-row">
                    <div class="pd-key">Enabled<div class="pd-sub">Poll this provider for usage</div></div>
                    <label class="toggle${p.enabled ? ' on' : ''}"><i></i><span>${p.enabled ? 'On' : 'Off'}</span></label>
                </div>
                <div class="pd-row">
                    <div class="pd-key">Poll interval<div class="pd-sub">How often to refresh usage</div></div>
                    <select class="pd-inp" id="sm-poll-select" style="width:auto">
                        <option value="" ${!p.poll_interval_seconds ? 'selected' : ''}>default</option>
                        ${pollOpts}
                    </select>
                </div>
            </div>
        </div>
        <div class="ios-group">
            <div class="ios-group-label">Configuration</div>
            <div class="ios-card">${buildProviderDetailHTML(p)}</div>
        </div>
        ${liveBtn}`;

    // Save / Discard / Raw-data / strategy wiring — shared with desktop; the
    // rerender callback keeps us on this pane instead of painting the list.
    wireProviderDetail(body, p, { rerender: () => _openProviderDetail(providerId, { refresh: true }) });

    // Enabled toggle (visual flip; persisted via the shared Save button,
    // which reads [data-drill-enabled] .toggle).
    body.querySelector('[data-drill-enabled] .toggle')?.addEventListener('click', function () {
        const on = this.classList.toggle('on');
        this.querySelector('span').textContent = on ? 'On' : 'Off';
    });

    // Poll interval auto-saves, matching the desktop row shortcut.
    body.querySelector('#sm-poll-select')?.addEventListener('change', async function () {
        try {
            await putProviderConfig(providerId, { poll_interval_seconds: parseInt(this.value, 10) || 0 });
        } catch (err) {
            console.warn('poll save failed', err);
        }
    });

    body.querySelector('#sm-live-usage')?.addEventListener('click', () => openProviderModal(fleetEntry));

    if (_navStack[_navStack.length - 1] !== 'provider') _push('provider');
}
