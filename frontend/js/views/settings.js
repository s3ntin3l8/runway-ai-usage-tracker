import { fetchProviderConfigs, putProviderConfig, fetchTokenHealth, postTokenRefresh, fetchSettings, fetchAppConfig, putAppConfig } from '../api.js';
import { STATE } from '../state.js';
import { ensureSortable } from '../sortable.js';

let _expandedProviderId = localStorage.getItem('settings_expanded_provider') || null;
let _providerCount = null;
let _tokenCount = null;
let _webhookCount = null;

const POLL_OPTIONS = [
    { label: '1 min',   value: 60 },
    { label: '5 min',   value: 300 },
    { label: '15 min',  value: 900 },
    { label: '30 min',  value: 1800 },
    { label: '1 hour',  value: 3600 },
];

const FRIENDLY_TOKEN_TYPES = {
    'api_key': 'API Key',
    'session_cookie': 'Cookie',
    'oauth_token': 'OAuth',
    'access_token': 'Token',
    'id_token': 'ID Token',
    'refresh_token': 'Refresh',
};

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

function escapeHTMLAttr(str) {
    if (!str) return '';
    return String(str).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function setExpanded(id) {
    _expandedProviderId = id;
    if (id) localStorage.setItem('settings_expanded_provider', id);
    else localStorage.removeItem('settings_expanded_provider');
}

function updateNavCounts() {
    if (_providerCount !== null) {
        const el = document.getElementById('sn-count-providers');
        if (el) el.textContent = _providerCount;
    }
    if (_tokenCount !== null) {
        const el = document.getElementById('sn-count-tokens');
        if (el) el.textContent = _tokenCount;
    }
    if (_webhookCount !== null) {
        const el = document.getElementById('sn-count-webhooks');
        if (el) el.textContent = _webhookCount;
    }
}

function flashRow(pane, providerId) {
    const row = pane.querySelector(`.provider-row[data-provider-id="${CSS.escape(providerId)}"]`);
    if (!row) return;
    row.classList.remove('pr-saved');
    // Force reflow to restart animation
    void row.offsetWidth;
    row.classList.add('pr-saved');
    setTimeout(() => row.classList.remove('pr-saved'), 750);
}

export function loadSettingsView() {
    _expandedProviderId = localStorage.getItem('settings_expanded_provider') || null;
    const activeSection = localStorage.getItem('settings_section') || 'providers';
    document.querySelectorAll('.settings-nav-item').forEach(btn => {
        btn.classList.toggle('on', btn.dataset.section === activeSection);
        btn.addEventListener('click', () => switchSettingsSection(btn.dataset.section));
    });
    renderSettingsSection(activeSection);
    updateNavCounts();
    prefetchNavCounts(activeSection);
}

async function prefetchNavCounts(activeSection) {
    const tasks = [];
    if (activeSection !== 'providers') {
        tasks.push(fetchProviderConfigs()
            .then(({ providers }) => { _providerCount = providers.length; })
            .catch(() => {}));
    }
    if (activeSection !== 'tokens') {
        tasks.push(fetchTokenHealth()
            .then(h => { _tokenCount = (h.tokens || []).length; })
            .catch(() => {}));
    }
    if (activeSection !== 'webhooks') {
        tasks.push(fetch('/api/v1/system/webhooks')
            .then(r => r.json())
            .then(d => { _webhookCount = (d.webhooks || []).length; })
            .catch(() => {}));
    }
    await Promise.all(tasks);
    updateNavCounts();
}

export function switchSettingsSection(name) {
    localStorage.setItem('settings_section', name);
    document.querySelectorAll('.settings-nav-item').forEach(btn => {
        btn.classList.toggle('on', btn.dataset.section === name);
    });
    renderSettingsSection(name);
}

function renderSettingsSection(name) {
    const pane = document.getElementById('settings-pane');
    if (!pane) return;
    pane.innerHTML = '<p style="padding:20px;color:var(--ink-3);font-size:10px;letter-spacing:0.1em;text-transform:uppercase;">Loading…</p>';
    if (name === 'providers') renderProvidersSection(pane);
    else if (name === 'tokens') renderTokensSection(pane);
    else if (name === 'webhooks') renderWebhooksSection(pane);
    else if (name === 'system') renderSystemSection(pane);
}

// ─── Providers ───────────────────────────────────────────────────────────────

export async function renderProvidersSection(pane) {
    try {
        const { providers } = await fetchProviderConfigs();

        if (_expandedProviderId && !providers.some(p => p.provider_id === _expandedProviderId)) {
            setExpanded(null);
        }
        _providerCount = providers.length;
        updateNavCounts();

        const rowsHTML = providers.length
            ? providers.map(p => buildProviderRowHTML(p)).join('')
            : '<p style="padding:16px;color:var(--ink-3);font-size:11px;">No providers configured.</p>';

        pane.innerHTML = `<div class="settings-panel glass">
            <header class="sp-head">
                <div>
                    <h3>Providers</h3>
                    <p>Enable / disable collection and tune poll intervals per provider.</p>
                </div>
                <button class="btn-ghost" id="providers-refresh-btn">Refresh all</button>
            </header>
            <div id="provider-list">${rowsHTML}</div>
        </div>`;

        // Accordion toggle
        pane.querySelectorAll('.provider-row-header').forEach(header => {
            header.addEventListener('click', (e) => {
                if (e.target.closest('.pr-poll') || e.target.closest('.toggle')) return;
                const providerId = header.dataset.providerId;
                setExpanded(_expandedProviderId === providerId ? null : providerId);
                renderProvidersSection(pane);
            });
        });

        // Poll interval auto-save
        pane.querySelectorAll('.pr-poll select').forEach(sel => {
            sel.addEventListener('change', async () => {
                const row = sel.closest('.provider-row');
                const providerId = row?.dataset.providerId;
                if (!providerId) return;
                const prev = providers.find(p => p.provider_id === providerId)?.poll_interval_seconds ?? null;
                const value = parseInt(sel.value, 10) || 0;
                try {
                    await putProviderConfig(providerId, { poll_interval_seconds: value });
                    flashRow(pane, providerId);
                } catch (err) {
                    sel.value = prev ?? '';
                    alert(`Save failed: ${err.message}`);
                }
            });
        });

        // Enabled toggle auto-save
        pane.querySelectorAll('.provider-row-header .toggle').forEach(toggle => {
            toggle.addEventListener('click', async (e) => {
                e.stopPropagation();
                const row = toggle.closest('.provider-row');
                const providerId = row?.dataset.providerId;
                if (!providerId) return;
                const newEnabled = !toggle.classList.contains('on');
                toggle.classList.toggle('on', newEnabled);
                toggle.querySelector('span').textContent = newEnabled ? 'On' : 'Off';
                try {
                    await putProviderConfig(providerId, { enabled: newEnabled });
                    flashRow(pane, providerId);
                    if (!newEnabled && _expandedProviderId === providerId) {
                        setExpanded(null);
                        renderProvidersSection(pane);
                    }
                } catch (err) {
                    toggle.classList.toggle('on', !newEnabled);
                    toggle.querySelector('span').textContent = !newEnabled ? 'On' : 'Off';
                    alert(`Save failed: ${err.message}`);
                }
            });
        });

        // Wire the expanded detail form
        const selected = providers.find(p => p.provider_id === _expandedProviderId);
        if (selected) wireProviderDetail(pane, selected);

        pane.querySelector('#providers-refresh-btn')?.addEventListener('click', () => renderProvidersSection(pane));

    } catch (err) {
        pane.innerHTML = `<div class="settings-panel glass"><p style="padding:20px;color:var(--crit);font-size:11px;">Failed to load providers: ${escapeHTML(err.message)}</p></div>`;
    }
}

function formatEffectivePoll(p) {
    const secs = p.effective_poll_interval;
    const label = POLL_OPTIONS.find(o => o.value === secs)?.label ?? `${secs}s`;
    const src = p.poll_interval_source === 'provider_override' ? 'override'
              : p.poll_interval_source === 'global_override'   ? 'global'
              : 'default';
    return `every ${label} · ${src}`;
}

function buildProviderRowHTML(p) {
    const isExpanded = p.provider_id === _expandedProviderId;
    const pollOpts = POLL_OPTIONS.map(o =>
        `<option value="${o.value}" ${p.poll_interval_seconds === o.value ? 'selected' : ''}>${o.label}</option>`
    ).join('');

    const detail = isExpanded
        ? `<div class="provider-detail">${buildProviderDetailHTML(p)}</div>`
        : '';

    return `<div class="provider-row${isExpanded ? ' expanded' : ''}" data-provider-id="${escapeHTMLAttr(p.provider_id)}">
        <div class="provider-row-header" data-provider-id="${escapeHTMLAttr(p.provider_id)}">
            <div class="plogo c-${escapeHTMLAttr(p.provider_id)}">${escapeHTML(p.icon || (p.name?.[0] ?? '?'))}</div>
            <div>
                <div class="pr-name">${escapeHTML(p.name)}</div>
                <div class="pr-meta">${escapeHTML(formatEffectivePoll(p))}</div>
            </div>
            <div class="pr-poll">poll
                <select>
                    <option value="" ${!p.poll_interval_seconds ? 'selected' : ''}>default</option>
                    ${pollOpts}
                </select>
            </div>
            <label class="toggle${p.enabled ? ' on' : ''}">
                <i></i><span>${p.enabled ? 'On' : 'Off'}</span>
            </label>
        </div>
        ${detail}
    </div>`;
}

function buildProviderDetailHTML(p) {
    const defaultTtlLabel = POLL_OPTIONS.find(o => o.value === p.default_ttl_seconds)?.label ?? `${p.default_ttl_seconds}s`;

    const apiKeyHTML = p.supports_api_key ? `<div class="pd-row">
        <div>
            <div class="pd-key">API Key</div>
            ${!p.api_key_set ? '<div class="pd-sub">No key stored — env var / file / keychain used as fallback.</div>' : ''}
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:flex-end;">
            <div id="api-key-display"${p.api_key_set ? '' : ' style="display:none"'}>
                <span style="font-size:10px;color:var(--ink-3);">••••••••••••••••</span>
            </div>
            <div id="api-key-input-row"${p.api_key_set ? ' style="display:none"' : ''}>
                <input type="text" id="field-api-key" placeholder="${p.api_key_set ? 'Leave blank to keep current' : 'Enter API key'}" class="pd-inp">
            </div>
            <button type="button" id="api-key-edit-btn" class="btn-ghost" style="padding:4px 10px;font-size:9px;">Edit</button>
        </div>
    </div>` : '';

    const cookieHTML = p.supports_session_cookie ? `<div class="pd-row">
        <div>
            <div class="pd-key">${escapeHTML(p.session_cookie_label || 'Session Cookie')}</div>
            <div class="pd-sub">${escapeHTML(p.session_cookie_help || 'Manual override — bypasses browser cookie extraction')}</div>
            ${!p.session_cookie_set ? '<div class="pd-sub" style="margin-top:2px;">No cookie stored — browser extraction used as fallback.</div>' : ''}
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:flex-end;">
            <div id="session-cookie-display"${p.session_cookie_set ? '' : ' style="display:none"'}>
                <span style="font-size:10px;color:var(--ink-3);">••••••••••••••••</span>
            </div>
            <div id="session-cookie-input-row"${p.session_cookie_set ? ' style="display:none"' : ''}>
                <input type="text" id="field-session-cookie" placeholder="${p.session_cookie_set ? 'Leave blank to keep current' : 'Paste session cookie value'}" class="pd-inp">
            </div>
            <button type="button" id="session-cookie-edit-btn" class="btn-ghost" style="padding:4px 10px;font-size:9px;">Edit</button>
        </div>
    </div>` : '';

    const oauthHTML = p.provider_id === 'github' ? `<div class="pd-row">
        <div>
            <div class="pd-key">GitHub OAuth</div>
            <div class="pd-sub">${STATE.githubAuth?.authenticated
                ? `Connected as ${escapeHTML(STATE.githubAuth.account || STATE.githubAuth.name || 'Account')}${STATE.githubAuth.email ? ` (${escapeHTML(STATE.githubAuth.email)})` : ''}`
                : 'Not connected'
            }</div>
        </div>
        ${STATE.githubAuth?.authenticated
            ? `<button type="button" onclick="handleGitHubLogout()" class="btn-ghost" style="padding:4px 10px;font-size:9px;color:var(--crit);">Disconnect</button>`
            : `<button type="button" onclick="startGitHubLogin()" class="btn-ghost" style="padding:4px 10px;font-size:9px;">Connect</button>`
        }
    </div>` : '';

    const strategies = p.supported_strategies || [];
    let strategyHTML = '';
    if (strategies.length > 1) {
        const resolvedList = buildResolvedStrategyList(strategies, p.collection_strategies || null);
        const rows = resolvedList.map(s => `
            <div class="strategy-item" data-strategy-id="${escapeHTMLAttr(s.id)}">
                <span class="strategy-drag-handle" title="Drag to reorder">⠿</span>
                <span class="strategy-label">${escapeHTML(s.label)}</span>
                <label class="toggle${s.enabled ? ' on' : ''}">
                    <i></i><span>${s.enabled ? 'On' : 'Off'}</span>
                </label>
            </div>`).join('');
        strategyHTML = `<div class="pd-row" style="display:block;">
            <div class="pd-key">Data Sources</div>
            <div class="pd-sub">Drag to reorder · toggle to enable/disable</div>
            <div id="strategy-list" class="strategy-list">${rows}</div>
        </div>`;
    }

    return `<div id="provider-config-form">
        ${apiKeyHTML}
        ${cookieHTML}
        ${oauthHTML}
        <div class="pd-row">
            <div class="pd-key">Account Label</div>
            <input type="text" id="field-account-label" value="${escapeHTMLAttr(p.account_label || '')}" placeholder="Auto-detected" class="pd-inp">
        </div>
        <div class="pd-row">
            <div>
                <div class="pd-key">Poll Interval</div>
                <div class="pd-sub">Effective: ${p.effective_poll_interval}s (${p.poll_interval_source}) · default: ${defaultTtlLabel}</div>
            </div>
            <span style="font-size:10px;color:var(--ink-3);">use row shortcut to override</span>
        </div>
        ${strategyHTML}
        <div class="pd-footer">
            <button type="button" id="provider-raw-data-btn" class="btn-ghost" style="padding:4px 10px;font-size:9px;">View Raw Data</button>
            <div style="display:flex;gap:8px;">
                <button type="button" id="provider-discard-btn" class="btn-ghost" style="padding:4px 10px;font-size:9px;">Discard</button>
                <button type="button" id="provider-save-btn" class="btn-primary" style="padding:4px 10px;font-size:9px;">Save</button>
            </div>
        </div>
    </div>`;
}

function wireProviderDetail(pane, selected) {
    pane.querySelector('#provider-save-btn')?.addEventListener('click', () =>
        saveProviderConfig(pane, selected.provider_id)
    );
    pane.querySelector('#provider-discard-btn')?.addEventListener('click', () =>
        renderProvidersSection(pane)
    );
    pane.querySelector('#provider-raw-data-btn')?.addEventListener('click', () => {
        if (typeof window.viewRawProviderData === 'function') {
            window.viewRawProviderData(selected.provider_id);
        }
    });
    pane.querySelector('#api-key-edit-btn')?.addEventListener('click', () => toggleFieldEdit(pane, 'api-key'));
    pane.querySelector('#session-cookie-edit-btn')?.addEventListener('click', () => toggleFieldEdit(pane, 'session-cookie'));

    // Strategy toggles
    pane.querySelectorAll('#strategy-list .toggle').forEach(toggle => {
        toggle.addEventListener('click', function () {
            const newEnabled = !this.classList.contains('on');
            this.classList.toggle('on', newEnabled);
            this.querySelector('span').textContent = newEnabled ? 'On' : 'Off';
        });
    });

    // Sortable drag-reorder
    const strategyList = pane.querySelector('#strategy-list');
    if (strategyList) {
        ensureSortable().then(Sortable => {
            Sortable.create(strategyList, {
                handle: '.strategy-drag-handle',
                animation: 150,
                ghostClass: 'opacity-30',
            });
        }).catch(err => console.warn('Could not load Sortable.js:', err));
    }
}

function toggleFieldEdit(pane, prefix) {
    const display = pane.querySelector(`#${prefix}-display`);
    const input = pane.querySelector(`#${prefix}-input-row`);
    if (!display || !input) return;
    const showing = input.style.display !== 'none';
    display.style.display = showing ? '' : 'none';
    input.style.display = showing ? 'none' : '';
}

async function saveProviderConfig(pane, providerId) {
    const btn = pane.querySelector('#provider-save-btn');
    if (btn) { btn.textContent = 'Saving…'; btn.disabled = true; }

    const expandedRow = pane.querySelector('.provider-row.expanded');
    const enabledToggle = expandedRow?.querySelector('.provider-row-header .toggle');
    const enabled = enabledToggle ? enabledToggle.classList.contains('on') : true;

    const accountLabel = pane.querySelector('#field-account-label')?.value ?? null;

    const apiKeyInput = pane.querySelector('#api-key-input-row');
    const apiKeyVisible = apiKeyInput && apiKeyInput.style.display !== 'none';
    const apiKey = apiKeyVisible ? (pane.querySelector('#field-api-key')?.value ?? '') : undefined;

    const cookieInput = pane.querySelector('#session-cookie-input-row');
    const cookieVisible = cookieInput && cookieInput.style.display !== 'none';
    const sessionCookie = cookieVisible ? (pane.querySelector('#field-session-cookie')?.value ?? '') : undefined;

    let collectionStrategies = null;
    const strategyList = pane.querySelector('#strategy-list');
    if (strategyList) {
        const rows = strategyList.querySelectorAll('.strategy-item');
        if (rows.length > 0) {
            collectionStrategies = Array.from(rows).map(row => ({
                id: row.dataset.strategyId,
                enabled: row.querySelector('.toggle')?.classList.contains('on') ?? true,
            }));
        }
    }

    try {
        await putProviderConfig(providerId, {
            enabled,
            ...(apiKey !== undefined ? { api_key: apiKey } : {}),
            ...(sessionCookie !== undefined ? { session_cookie: sessionCookie } : {}),
            account_label: accountLabel,
            ...(collectionStrategies !== null ? { collection_strategies: collectionStrategies } : {}),
        });
        renderProvidersSection(pane);
    } catch (err) {
        if (btn) { btn.textContent = 'Save'; btn.disabled = false; }
        alert(`Save failed: ${err.message}`);
    }
}

function buildResolvedStrategyList(supported, savedConfig) {
    if (!savedConfig || savedConfig.length === 0) {
        return supported.map(s => ({ ...s, enabled: true }));
    }
    const result = [];
    const supportedMap = Object.fromEntries(supported.map(s => [s.id, s]));
    const seenIds = new Set();
    for (const saved of savedConfig) {
        if (supportedMap[saved.id]) {
            result.push({ id: saved.id, label: supportedMap[saved.id].label, enabled: saved.enabled !== false });
            seenIds.add(saved.id);
        }
    }
    for (const s of supported) {
        if (!seenIds.has(s.id)) result.push({ id: s.id, label: s.label, enabled: true });
    }
    return result;
}

// ─── Tokens ──────────────────────────────────────────────────────────────────

async function renderTokensSection(pane) {
    try {
        const health = await fetchTokenHealth();
        const tokens = health.tokens || [];
        _tokenCount = tokens.length;
        updateNavCounts();

        const rowsHTML = tokens.length === 0
            ? '<p style="padding:16px;color:var(--ink-3);font-size:11px;">No active credentials in cache.</p>'
            : tokens.map(t => buildTokenRowHTML(t)).join('');

        pane.innerHTML = `<div class="settings-panel glass">
            <header class="sp-head">
                <div>
                    <h3>Token health</h3>
                    <p>OAuth tokens and browser cookies discovered by Runway.</p>
                </div>
                <button class="btn-ghost" id="tokens-refresh-btn">Refresh all</button>
            </header>
            <div class="token-list">${rowsHTML}</div>
        </div>`;

        pane.querySelectorAll('.tk-refresh[data-provider]:not([data-purge])').forEach(btn => {
            btn.addEventListener('click', () => refreshToken(btn.dataset.provider, btn.dataset.account));
        });
        pane.querySelectorAll('.tk-refresh[data-purge]').forEach(btn => {
            btn.addEventListener('click', () => deleteToken(btn.dataset.provider, btn.dataset.account));
        });
        pane.querySelector('#tokens-refresh-btn')?.addEventListener('click', () => renderTokensSection(pane));

    } catch (err) {
        pane.innerHTML = `<div class="settings-panel glass"><p style="padding:20px;color:var(--crit);font-size:11px;">Failed to load token health: ${escapeHTML(err.message)}</p></div>`;
    }
}

function buildTokenRowHTML(t) {
    // Expiry info
    let expiryLabel = 'no expiry', expiryCls = '', expiryPct = 100;
    if (t.status === 'expired') {
        expiryLabel = 'expired'; expiryCls = 'crit'; expiryPct = 0;
    } else if (t.expires_at) {
        const days = Math.ceil((new Date(t.expires_at) - Date.now()) / 86400000);
        if (days <= 0) { expiryLabel = 'expired'; expiryCls = 'crit'; expiryPct = 0; }
        else {
            expiryCls = days <= 2 ? 'crit' : days <= 14 ? 'warn' : '';
            expiryPct = Math.max(4, Math.min(100, days / 180 * 100));
            expiryLabel = days === 1 ? '1 day' : `${days} days`;
        }
    } else if (t.ttl_remaining_seconds > 0) {
        const days = Math.ceil(t.ttl_remaining_seconds / 86400);
        expiryCls = days <= 2 ? 'crit' : days <= 14 ? 'warn' : '';
        expiryPct = Math.max(4, Math.min(100, days / 180 * 100));
        expiryLabel = `~${days}d`;
    }

    // Kind for badge
    const types = t.token_types || [];
    let kind = 'local';
    if (types.some(k => k === 'oauth_token' || k === 'access_token')) kind = 'oauth';
    else if (types.some(k => k === 'api_key')) kind = 'api';
    else if (types.some(k => k.includes('cookie') || k.includes('session') || k.startsWith('COOKIE_') || k.startsWith('__Secure-'))) kind = 'web';

    // Friendly kind label
    const seenKinds = new Set();
    const kindLabels = [];
    for (const k of types) {
        let clean = FRIENDLY_TOKEN_TYPES[k] || k;
        if (k.startsWith('COOKIE_') || k.startsWith('__Secure-') || k.toLowerCase().includes('session')) clean = 'Cookie';
        if (!seenKinds.has(clean)) { kindLabels.push(clean); seenKinds.add(clean); }
    }
    const kindLabel = kindLabels[0] || 'Token';

    // Account display
    let account = t.account_label || t.account_id || '';
    if (['default', 'config', 'config-cookie'].includes(account)) account = '';

    const canRefresh = t.can_refresh && t.status !== 'valid';

    return `<div class="token-row">
        <div class="plogo c-${escapeHTMLAttr(t.provider)}">${escapeHTML(t.provider?.[0] ?? '?')}</div>
        <div>
            <div class="tk-name">${escapeHTML(t.provider)}</div>
            <div class="tk-acc">${account ? escapeHTML(account) + ' · ' : ''}${escapeHTML(kindLabel)}</div>
        </div>
        <div>
            <div class="tk-expiry${expiryCls ? ' ' + expiryCls : ''}">${escapeHTML(expiryLabel)}</div>
            <div class="tk-bar${expiryCls ? ' ' + expiryCls : ''}"><i style="width:${expiryPct}%"></i></div>
        </div>
        <span class="badge src-${kind}">${kind}</span>
        <div style="display:flex;gap:4px;align-items:center;justify-content:flex-end;">
            ${canRefresh ? `<button class="tk-refresh" data-provider="${escapeHTMLAttr(t.provider)}" data-account="${escapeHTMLAttr(t.account_id)}">↻</button>` : ''}
            <button class="tk-refresh" data-purge data-provider="${escapeHTMLAttr(t.provider)}" data-account="${escapeHTMLAttr(t.account_id)}" style="color:var(--crit);" title="Purge from cache">✕</button>
        </div>
    </div>`;
}

export async function refreshToken(provider, accountId) {
    try {
        const d = await postTokenRefresh(provider, accountId);
        if (d.status === 'refreshed') {
            const pane = document.getElementById('settings-pane');
            if (pane) renderTokensSection(pane);
        } else {
            alert('Refresh reported non-success: ' + JSON.stringify(d));
        }
    } catch (err) {
        alert('Token refresh failed: ' + err.message);
    }
}

export async function deleteToken(provider, accountId) {
    if (!confirm(`Purge the ${provider} session for ${accountId} from the live cache?`)) return;
    try {
        const res = await fetch(`/api/v1/system/token-health/${encodeURIComponent(provider)}/${encodeURIComponent(accountId)}`, {
            method: 'DELETE',
        });
        const d = await res.json();
        if (res.ok) {
            const pane = document.getElementById('settings-pane');
            if (pane) renderTokensSection(pane);
        } else {
            alert('Purge failed: ' + (d.detail || JSON.stringify(d)));
        }
    } catch (err) {
        alert('Delete failed: ' + err.message);
    }
}

// ─── Webhooks ─────────────────────────────────────────────────────────────────

async function renderWebhooksSection(pane) {
    let webhooks = [];
    try {
        const res = await fetch('/api/v1/system/webhooks');
        webhooks = (await res.json()).webhooks || [];
    } catch (e) { /* ignore */ }

    _webhookCount = webhooks.length;
    updateNavCounts();

    const cardsHTML = webhooks.length
        ? webhooks.map(w => webhookCardHtml(w)).join('')
        : '<p style="padding:16px;color:var(--ink-3);font-size:11px;">No webhook rules. Click "+ New rule" to add one.</p>';

    pane.innerHTML = `<div class="settings-panel glass">
        <header class="sp-head">
            <div>
                <h3>Webhooks · threshold alerts</h3>
                <p>Rules fire when any matching quota crosses its threshold.</p>
            </div>
            <button class="btn-primary" id="add-webhook-btn">+ New rule</button>
        </header>
        <div class="webhook-list" id="webhook-rows">${cardsHTML}</div>
    </div>`;

    wireWebhookCards(pane);
    pane.querySelector('#add-webhook-btn')?.addEventListener('click', () => addWebhookRow());
}

function webhookCardHtml(w) {
    const icon = w.channel === 'slack' ? '#' : 'D';
    const url = w.url || '';
    const shortUrl = url.length > 48 ? url.slice(0, 48) + '…' : url || '—';
    return `<div class="webhook-card" data-webhook-id="${w.id}">
        <div class="wh-head">
            <div class="wh-icon ${escapeHTMLAttr(w.channel)}">${icon}</div>
            <div style="flex:1;min-width:0;">
                <div class="wh-title">${escapeHTML(w.provider_id || '*')}</div>
                <div class="wh-url">${escapeHTML(shortUrl)}</div>
            </div>
            <label class="toggle on"><i></i><span>Active</span></label>
        </div>
        <div class="wh-rule">
            <input type="text" data-field="provider_id" value="${escapeHTMLAttr(w.provider_id)}" placeholder="provider or *" class="inp" style="width:80px;">
            <input type="number" data-field="threshold_pct" value="${w.threshold_pct}" min="1" max="100" step="1" class="inp" style="width:52px;">
            <span style="color:var(--ink-3);font-size:10px;">%</span>
            <select data-field="channel" class="inp" style="width:auto;">
                <option value="discord" ${w.channel === 'discord' ? 'selected' : ''}>Discord</option>
                <option value="slack" ${w.channel === 'slack' ? 'selected' : ''}>Slack</option>
            </select>
            <input type="url" data-field="url" value="${escapeHTMLAttr(url)}" placeholder="Webhook URL" class="inp" style="flex:1;min-width:120px;">
            <button class="btn-ghost wh-test" style="padding:4px 10px;font-size:9px;">Test</button>
            <button class="btn-ghost wh-delete" style="padding:4px 10px;font-size:9px;color:var(--crit);">✕</button>
        </div>
    </div>`;
}

function wireWebhookCards(pane) {
    pane.querySelectorAll('.webhook-card').forEach(card => {
        const webhookId = parseInt(card.dataset.webhookId, 10);
        card.querySelectorAll('[data-field]').forEach(el => {
            el.addEventListener('change', async () => {
                const field = el.dataset.field;
                const value = el.type === 'number' ? parseFloat(el.value) : el.value;
                await patchWebhook(webhookId, field, value);
                // Sync header display
                if (field === 'provider_id') {
                    const title = card.querySelector('.wh-title');
                    if (title) title.textContent = el.value || '*';
                } else if (field === 'url') {
                    const urlEl = card.querySelector('.wh-url');
                    if (urlEl) urlEl.textContent = el.value.length > 48 ? el.value.slice(0, 48) + '…' : (el.value || '—');
                } else if (field === 'channel') {
                    const ico = card.querySelector('.wh-icon');
                    if (ico) { ico.className = `wh-icon ${el.value}`; ico.textContent = el.value === 'slack' ? '#' : 'D'; }
                }
            });
        });
        card.querySelector('.wh-test')?.addEventListener('click', () => testWebhook(webhookId));
        card.querySelector('.wh-delete')?.addEventListener('click', () => deleteWebhook(webhookId));
    });
}

export async function addWebhookRow() {
    const res = await fetch('/api/v1/system/webhooks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider_id: '*', threshold_pct: 90, url: '', channel: 'discord' }),
    });
    if (res.ok) {
        const pane = document.getElementById('settings-pane');
        if (pane) renderWebhooksSection(pane);
    }
}

export async function patchWebhook(id, field, value) {
    await fetch(`/api/v1/system/webhooks/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: value }),
    });
}

export async function testWebhook(id) {
    const res = await fetch(`/api/v1/system/webhooks/${id}/test`, { method: 'POST' });
    const data = await res.json();
    alert(res.ok ? 'Test sent!' : `Failed: ${data.detail}`);
}

export async function deleteWebhook(id) {
    await fetch(`/api/v1/system/webhooks/${id}`, { method: 'DELETE' });
    const pane = document.getElementById('settings-pane');
    if (pane) renderWebhooksSection(pane);
}

// ─── System ───────────────────────────────────────────────────────────────────

async function renderSystemSection(pane) {
    try {
        const [s, cfg] = await Promise.all([fetchSettings(), fetchAppConfig()]);
        const browserPref = escapeHTMLAttr(cfg.browser_preference || '');
        const globalPollVal = cfg.default_poll_interval_seconds ?? '';
        const localCollectorOn = cfg.local_collector_enabled;
        const credScrapingOn = cfg.local_credential_scraping_enabled;
        const pollSelectOpts = POLL_OPTIONS.map(o =>
            `<option value="${o.value}" ${globalPollVal === o.value ? 'selected' : ''}>${o.label}</option>`
        ).join('');

        pane.innerHTML = `<div class="settings-panel glass">
            <header class="sp-head">
                <div>
                    <h3>System</h3>
                    <p>Global configuration · overrides environment variables.</p>
                </div>
            </header>
            <div class="sys-grid">
                <div class="sys-row">
                    <div><div class="sys-k">Run Mode</div></div>
                    <span class="inp" style="display:block;width:auto;">${escapeHTML(s.run_mode)}</span>
                </div>
                <div class="sys-row">
                    <div><div class="sys-k">Host / Port</div></div>
                    <span style="font-size:12px;color:var(--ink);">${escapeHTML(s.app_host)}:${s.app_port}</span>
                </div>
                <div class="sys-row">
                    <div>
                        <div class="sys-k">Local Collectors</div>
                        <div class="sys-s">Enable reading local files and DBs (CLI tools, logs)</div>
                    </div>
                    <label class="toggle${localCollectorOn ? ' on' : ''}" data-cfg-key="local_collector_enabled">
                        <i></i><span>${localCollectorOn ? 'Enabled' : 'Disabled'}</span>
                    </label>
                </div>
                <div class="sys-row">
                    <div>
                        <div class="sys-k">Credential Scraping</div>
                        <div class="sys-s">Allow reading browser cookies and credential files</div>
                    </div>
                    <label class="toggle${credScrapingOn ? ' on' : ''}" data-cfg-key="local_credential_scraping_enabled">
                        <i></i><span>${credScrapingOn ? 'Enabled' : 'Disabled'}</span>
                    </label>
                </div>
                <div class="sys-row">
                    <div><div class="sys-k">Database Encryption</div></div>
                    <span style="font-size:11px;color:${s.encryption_enabled ? 'var(--good)' : 'var(--warn)'};">${s.encryption_enabled ? '✅ Active' : '🔓 Plaintext'}</span>
                </div>
                ${!s.encryption_enabled ? `<div class="sys-row">
                    <div class="sys-s" style="color:var(--warn);">Set <code>DB_ENCRYPTION_KEY</code> env var to secure your snapshots.</div>
                    <div></div>
                </div>` : ''}
                <div class="sys-row">
                    <div>
                        <div class="sys-k">Default Poll Interval</div>
                        <div class="sys-s">Applies to all providers; per-provider overrides take precedence</div>
                    </div>
                    <div style="display:flex;gap:8px;align-items:center;">
                        <select id="field-global-poll" class="inp" style="width:auto;">
                            <option value="" ${!globalPollVal ? 'selected' : ''}>Per-collector default</option>
                            ${pollSelectOpts}
                        </select>
                        <button id="save-global-poll-btn" class="btn-ghost" style="padding:4px 10px;font-size:9px;white-space:nowrap;">Save</button>
                    </div>
                </div>
                <div class="sys-row">
                    <div>
                        <div class="sys-k">Browser Preference</div>
                        <div class="sys-s">Cookie-auth order for Claude web, ChatGPT, Ollama… (e.g. safari,chrome,firefox)</div>
                    </div>
                    <div style="display:flex;gap:8px;align-items:center;">
                        <input id="field-browser-pref" type="text" value="${browserPref}" class="inp" placeholder="safari,chrome,chromium,edge,firefox">
                        <button id="save-browser-pref-btn" class="btn-ghost" style="padding:4px 10px;font-size:9px;white-space:nowrap;">Save</button>
                    </div>
                </div>
            </div>
            <div class="glass" style="margin-top:16px;padding:14px;border-left:2px solid var(--accent);font-size:11px;line-height:1.6;">
                <strong>Tip:</strong> Core configuration is still managed via <code style="background:var(--surface-2);padding:1px 5px;color:var(--accent);">.env</code>. Provider-specific overrides can be set in the Providers section above.
            </div>
        </div>`;

        // System toggles
        pane.querySelectorAll('.toggle[data-cfg-key]').forEach(toggle => {
            toggle.addEventListener('click', async function () {
                const cfgKey = this.dataset.cfgKey;
                const newVal = !this.classList.contains('on');
                this.classList.toggle('on', newVal);
                this.querySelector('span').textContent = newVal ? 'Enabled' : 'Disabled';
                try {
                    await putAppConfig({ [cfgKey]: newVal });
                } catch (err) {
                    this.classList.toggle('on', !newVal);
                    this.querySelector('span').textContent = !newVal ? 'Enabled' : 'Disabled';
                    alert(`Save failed: ${err.message}`);
                }
            });
        });

        pane.querySelector('#save-global-poll-btn')?.addEventListener('click', async function () {
            const select = pane.querySelector('#field-global-poll');
            const val = select?.value ? parseInt(select.value, 10) : 0;
            this.textContent = 'Saving…'; this.disabled = true;
            try {
                await putAppConfig({ default_poll_interval_seconds: val });
                this.textContent = 'Saved';
                setTimeout(() => { this.textContent = 'Save'; this.disabled = false; }, 1500);
            } catch {
                this.textContent = 'Error'; this.disabled = false;
            }
        });

        pane.querySelector('#save-browser-pref-btn')?.addEventListener('click', async function () {
            const input = pane.querySelector('#field-browser-pref');
            const val = input?.value.trim() || null;
            this.textContent = 'Saving…'; this.disabled = true;
            try {
                await putAppConfig({ browser_preference: val });
                this.textContent = 'Saved';
                setTimeout(() => { this.textContent = 'Save'; this.disabled = false; }, 1500);
            } catch {
                this.textContent = 'Error'; this.disabled = false;
            }
        });

    } catch (err) {
        pane.innerHTML = `<div class="settings-panel glass"><p style="padding:20px;color:var(--crit);font-size:11px;">Failed to load system info: ${escapeHTML(err.message)}</p></div>`;
    }
}

export function initSettingsView() {
    // Event listeners are attached after each section render
}
