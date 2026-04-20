import { fetchProviderConfigs, putProviderConfig, fetchTokenHealth, postTokenRefresh, fetchSettings, fetchAppConfig, putAppConfig } from '../api.js';
import { STATE } from '../state.js';
import { buildTokenHealthPanel } from '../components.js';
import { ensureSortable } from '../sortable.js';

let _settingsSelectedProvider = null;
let _settingsProviderOriginal = null;

const POLL_OPTIONS = [
    { label: '1 min',   value: 60 },
    { label: '5 min',   value: 300 },
    { label: '15 min',  value: 900 },
    { label: '30 min',  value: 1800 },
    { label: '1 hour',  value: 3600 },
];

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&gt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

function escapeHTMLAttr(str) {
    if (!str) return '';
    return String(str).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

export function loadSettingsView() {
    const activeSection = localStorage.getItem('settings_section') || 'providers';
    document.querySelectorAll('.settings-nav-item').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.section === activeSection);
        btn.addEventListener('click', () => switchSettingsSection(btn.dataset.section));
    });
    renderSettingsSection(activeSection);
}

export function switchSettingsSection(name) {
    localStorage.setItem('settings_section', name);
    document.querySelectorAll('.settings-nav-item').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.section === name);
    });
    renderSettingsSection(name);
}

function renderSettingsSection(name) {
    const pane = document.getElementById('settings-pane');
    if (!pane) return;
    pane.innerHTML = '<p class="text-zinc-500 animate-pulse text-sm">Loading…</p>';
    if (name === 'providers') renderProvidersSection(pane);
    else if (name === 'tokens') renderTokensSection(pane);
    else if (name === 'webhooks') renderWebhooksSection(pane);
    else if (name === 'system') renderSystemSection(pane);
}

export async function renderProvidersSection(pane) {
    try {
        const { providers } = await fetchProviderConfigs();
        
        // Load from localStorage if not set in memory
        if (!_settingsSelectedProvider) {
            _settingsSelectedProvider = localStorage.getItem('settings_selected_provider');
        }

        // If still not set, or no longer valid, default to first provider
        if (providers.length > 0) {
            const exists = providers.some(p => p.provider_id === _settingsSelectedProvider);
            if (!exists) {
                _settingsSelectedProvider = providers[0].provider_id;
            }
        }

        pane.innerHTML = buildProvidersSectionHTML(providers);
        pane.querySelectorAll('.provider-list-item').forEach(item => {
            item.addEventListener('click', () => {
                _settingsSelectedProvider = item.dataset.providerId;
                localStorage.setItem('settings_selected_provider', _settingsSelectedProvider);
                renderProvidersSection(pane);
            });
        });
        const form = pane.querySelector('#provider-config-form');
        if (form) {
            const selected = providers.find(p => p.provider_id === _settingsSelectedProvider);
            _settingsProviderOriginal = selected ? { ...selected } : null;

            pane.querySelector('#provider-save-btn')?.addEventListener('click', () => saveProviderConfig(pane, form, _settingsSelectedProvider));
            pane.querySelector('#provider-discard-btn')?.addEventListener('click', () => renderProvidersSection(pane));
            pane.querySelector('#provider-raw-data-btn')?.addEventListener('click', () => {
                if (typeof window.viewRawProviderData === 'function') {
                    window.viewRawProviderData(_settingsSelectedProvider);
                } else {
                    console.error('viewRawProviderData is not defined');
                }
            });
            pane.querySelector('#field-enabled-toggle')?.addEventListener('click', function() {
                const next = this.dataset.enabled !== 'true';
                this.dataset.enabled = String(next);
                this.classList.toggle('bg-violet-600', next);
                this.classList.toggle('bg-zinc-700', !next);
                this.querySelector('span').classList.toggle('translate-x-5', next);
                this.querySelector('span').classList.toggle('translate-x-0', !next);
            });
            pane.querySelector('#api-key-edit-btn')?.addEventListener('click', () => toggleApiKeyEdit(pane));
            pane.querySelector('#session-cookie-edit-btn')?.addEventListener('click', () => toggleSessionCookieEdit(pane));

            // Wire strategy toggle buttons
            pane.querySelectorAll('.strategy-toggle').forEach(btn => {
                btn.addEventListener('click', function() {
                    const next = this.dataset.enabled !== 'true';
                    this.dataset.enabled = String(next);
                    this.classList.toggle('bg-violet-600', next);
                    this.classList.toggle('bg-zinc-700', !next);
                    this.querySelector('span').classList.toggle('translate-x-4', next);
                    this.querySelector('span').classList.toggle('translate-x-0', !next);
                });
            });

            // Wire drag-to-reorder on strategy list via SortableJS
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
    } catch (err) {
        pane.innerHTML = `<p class="text-red-400 text-sm">Failed to load providers: ${escapeHTML(err.message)}</p>`;
    }
}

export function buildProvidersSectionHTML(providers) {
    const listHTML = providers.map(p => {
        const isSelected = p.provider_id === _settingsSelectedProvider;
        const badge = p.enabled
            ? '<span class="text-[9px] text-green-400 mono">ON</span>'
            : '<span class="text-[9px] text-zinc-600 mono">OFF</span>';
        return `<button class="provider-list-item w-full text-left flex items-center gap-2 px-3 py-2 rounded-xl transition-colors ${isSelected ? 'bg-zinc-700/50 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/40 hover:text-zinc-300'}" data-provider-id="${escapeHTMLAttr(p.provider_id)}">
            <span class="text-base leading-none">${p.icon}</span>
            <span class="text-xs font-medium truncate flex-1">${escapeHTML(p.name)}</span>
            ${badge}
        </button>`;
    }).join('');

    const selected = providers.find(p => p.provider_id === _settingsSelectedProvider);
    const formHTML = selected ? buildProviderForm(selected) : '<p class="text-zinc-500 text-sm">Select a provider.</p>';

    return `<div class="flex gap-4 h-full">
        <div class="w-36 shrink-0 flex flex-col gap-0.5 overflow-y-auto">
            <p class="text-[10px] uppercase tracking-widest text-zinc-600 px-3 mb-1">Providers</p>
            ${listHTML}
        </div>
        <div class="flex-1 min-w-0">${formHTML}</div>
    </div>`;
}

function buildProviderForm(p) {
    const defaultTtlLabel = POLL_OPTIONS.find(o => o.value === p.default_ttl_seconds)?.label || `${p.default_ttl_seconds}s`;
    const pollSelectOpts = POLL_OPTIONS.map(o => `<option value="${o.value}" ${p.poll_interval_seconds === o.value ? 'selected' : ''}>${o.label}</option>`).join('');

    // Build strategy rows (only for providers that declare strategies)
    const strategies = p.supported_strategies || [];
    let strategyHTML = '';
    if (strategies.length > 1) {
        // Merge saved user config with supported list to determine order + enabled state
        const savedConfig = p.collection_strategies || null;
        const resolvedList = buildResolvedStrategyList(strategies, savedConfig);

        const rows = resolvedList.map((s, i) => {
            const isEnabled = s.enabled;
            return `<div class="strategy-row flex items-center gap-2 py-2 px-1 border-b border-zinc-800/40" data-strategy-id="${escapeHTMLAttr(s.id)}">
                <span class="strategy-drag-handle text-zinc-500 text-sm select-none cursor-grab active:cursor-grabbing px-1.5" title="Drag to reorder">⠿</span>
                <div class="flex-1 min-w-0 truncate">
                    <span class="text-xs text-zinc-300 font-medium">${escapeHTML(s.label)}</span>
                </div>
                <button type="button" class="strategy-toggle relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${isEnabled ? 'bg-violet-600' : 'bg-zinc-700'}" data-enabled="${isEnabled}">
                    <span class="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${isEnabled ? 'translate-x-4' : 'translate-x-0'}"></span>
                </button>
            </div>`;
        }).join('');

        strategyHTML = `<div class="py-3 border-b border-zinc-800/50">
            <div class="mb-2 px-1">
                <span class="text-sm text-zinc-400">Data Sources</span>
                <p class="text-[10px] text-zinc-600 mt-0.5">Drag to reorder · toggle to enable/disable</p>
            </div>
            <div id="strategy-list" class="space-y-0.5">${rows}</div>
        </div>`;
    }

    return `<form id="provider-config-form" class="space-y-4 min-w-0 overflow-x-hidden px-1" onsubmit="return false">
        <h3 class="text-base font-semibold text-zinc-100 flex items-center gap-2"><span>${p.icon}</span> ${escapeHTML(p.name)}</h3>
        <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
            <div><span class="text-sm text-zinc-400">Enabled</span><p class="text-[10px] text-zinc-600 mt-0.5">Polling active for this provider</p></div>
            <button type="button" id="field-enabled-toggle" data-enabled="${p.enabled}" class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${p.enabled ? 'bg-violet-600' : 'bg-zinc-700'}">
                <span class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${p.enabled ? 'translate-x-5' : 'translate-x-0'}"></span>
            </button>
        </div>
        ${strategyHTML}
        ${p.supports_api_key ? `<div class="py-3 border-b border-zinc-800/50">
            <div class="flex items-center justify-between mb-2"><span class="text-sm text-zinc-400">API Key</span><button type="button" id="api-key-edit-btn" class="toggle-btn text-xs">Edit</button></div>
            <div id="api-key-display" class="${p.api_key_set ? '' : 'hidden'}"><span class="mono text-xs text-zinc-500">••••••••••••••••</span></div>
            <div id="api-key-input-row" class="${p.api_key_set ? 'hidden' : ''}"><input type="text" id="field-api-key" placeholder="${p.api_key_set ? 'Leave blank to keep current key' : 'Enter API key'}" class="w-full mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-zinc-200 focus:outline-none focus:border-violet-500"></div>
            ${!p.api_key_set ? '<p class="text-[10px] text-zinc-600 mt-1">No key stored — env var / file / keychain used as fallback.</p>' : ''}
        </div>` : ''}
        ${p.supports_session_cookie ? `<div class="py-3 border-b border-zinc-800/50">
            <div class="flex items-center justify-between mb-2"><div><span class="text-sm text-zinc-400">Session Cookie</span><p class="text-[10px] text-zinc-600 mt-0.5">Manual override — bypasses browser cookie extraction</p></div><button type="button" id="session-cookie-edit-btn" class="toggle-btn text-xs">Edit</button></div>
            <div id="session-cookie-display" class="${p.session_cookie_set ? '' : 'hidden'}"><span class="mono text-xs text-zinc-500">••••••••••••••••</span></div>
            <div id="session-cookie-input-row" class="${p.session_cookie_set ? 'hidden' : ''}"><input type="text" id="field-session-cookie" placeholder="${p.session_cookie_set ? 'Leave blank to keep current value' : 'Paste session cookie value'}" class="w-full mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-zinc-200 focus:outline-none focus:border-violet-500"></div>
            ${!p.session_cookie_set ? '<p class="text-[10px] text-zinc-600 mt-1">No cookie stored — browser extraction used as fallback.</p>' : ''}
        </div>` : ''}
        ${p.provider_id === 'github' ? `<div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                <div>
                    <span class="text-sm text-zinc-400">GitHub OAuth</span>
                    <p class="text-[10px] text-zinc-600 mt-0.5">${STATE.githubAuth?.authenticated ? `Connected as <span class="text-zinc-400">${escapeHTML(STATE.githubAuth.account || STATE.githubAuth.name || 'Account')}${STATE.githubAuth.email ? ` (${escapeHTML(STATE.githubAuth.email)})` : ''}</span>` : 'Not connected'}</p>
                </div>
  ${STATE.githubAuth?.authenticated ? `<button type="button" onclick="handleGitHubLogout()" class="toggle-btn text-xs text-red-400" style="border-color:#f87171">Disconnect</button>` : `<button type="button" onclick="startGitHubLogin()" class="toggle-btn text-xs">Connect</button>`}
        </div>` : ''}
        <label class="flex items-center justify-between py-3 border-b border-zinc-800/50"><span class="text-sm text-zinc-400">Account Label</span><input type="text" id="field-account-label" value="${escapeHTMLAttr(p.account_label || '')}" placeholder="Auto-detected" class="mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-1.5 text-zinc-200 w-48 focus:outline-none focus:border-violet-500"></label>
        <div class="flex items-center justify-between py-3 border-b border-zinc-800/50"><span class="text-sm text-zinc-400">Poll Interval Override</span><select id="field-poll-interval" class="mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-1.5 text-zinc-200 focus:outline-none focus:border-violet-500"><option value="" ${!p.poll_interval_seconds ? 'selected' : ''}>Default (${defaultTtlLabel})</option>${pollSelectOpts}</select></div>
        <div class="flex justify-between items-center pt-2">
            <button type="button" id="provider-raw-data-btn" class="toggle-btn text-xs" style="border-color:#3f3f46;color:#a1a1aa;">View Raw Data</button>
            <div class="flex gap-2"><button type="button" id="provider-discard-btn" class="toggle-btn text-xs">Discard</button><button type="button" id="provider-save-btn" class="toggle-btn text-xs" style="border-color:#7c3aed;color:#c4b5fd;">Save</button></div>
        </div>
    </form>`;
}

/** Build the merged and ordered strategy list for display. */
function buildResolvedStrategyList(supported, savedConfig) {
    if (!savedConfig || savedConfig.length === 0) {
        // No saved config — show all in default order, all enabled
        return supported.map(s => ({ ...s, enabled: true }));
    }

    // Start from saved order
    const result = [];
    const supportedMap = Object.fromEntries(supported.map(s => [s.id, s]));
    const seenIds = new Set();

    for (const saved of savedConfig) {
        if (supportedMap[saved.id]) {
            result.push({
                id: saved.id,
                label: supportedMap[saved.id].label,
                enabled: saved.enabled !== false,
            });
            seenIds.add(saved.id);
        }
    }

    // Append any new strategies not in saved config (enabled by default)
    for (const s of supported) {
        if (!seenIds.has(s.id)) {
            result.push({ id: s.id, label: s.label, enabled: true });
        }
    }

    return result;
}

function toggleApiKeyEdit(pane) {
    const display = pane.querySelector('#api-key-display');
    const input = pane.querySelector('#api-key-input-row');
    if (!display || !input) return;
    display.classList.toggle('hidden');
    input.classList.toggle('hidden');
}

function toggleSessionCookieEdit(pane) {
    const display = pane.querySelector('#session-cookie-display');
    const input = pane.querySelector('#session-cookie-input-row');
    if (!display || !input) return;
    display.classList.toggle('hidden');
    input.classList.toggle('hidden');
}

async function saveProviderConfig(pane, form, providerId) {
    const btn = pane.querySelector('#provider-save-btn');
    if (btn) { btn.textContent = 'Saving…'; btn.disabled = true; }

    const enabledToggle = pane.querySelector('#field-enabled-toggle');
    const enabled = enabledToggle ? enabledToggle.dataset.enabled === 'true' : true;
    const accountLabel = form.querySelector('#field-account-label')?.value ?? null;

    const apiKeyInputRow = pane.querySelector('#api-key-input-row');
    const apiKeyVisible = apiKeyInputRow && !apiKeyInputRow.classList.contains('hidden');
    const apiKey = apiKeyVisible ? (form.querySelector('#field-api-key')?.value ?? '') : undefined;

    const sessionCookieInputRow = pane.querySelector('#session-cookie-input-row');
    const sessionCookieVisible = sessionCookieInputRow && !sessionCookieInputRow.classList.contains('hidden');
    const sessionCookie = sessionCookieVisible ? (form.querySelector('#field-session-cookie')?.value ?? '') : undefined;

    const pollRaw = form.querySelector('#field-poll-interval')?.value;
    const pollIntervalSeconds = pollRaw ? parseInt(pollRaw, 10) : 0;

    // Read strategy ordering + enabled state from the DOM
    let collectionStrategies = null;
    const strategyList = pane.querySelector('#strategy-list');
    if (strategyList) {
        const rows = strategyList.querySelectorAll('.strategy-row');
        if (rows.length > 0) {
            collectionStrategies = Array.from(rows).map(row => ({
                id: row.dataset.strategyId,
                enabled: row.querySelector('.strategy-toggle')?.dataset.enabled === 'true',
            }));
        }
    }

    try {
        await putProviderConfig(providerId, {
            enabled,
            ...(apiKey !== undefined ? { api_key: apiKey } : {}),
            ...(sessionCookie !== undefined ? { session_cookie: sessionCookie } : {}),
            account_label: accountLabel,
            poll_interval_seconds: pollIntervalSeconds,
            ...(collectionStrategies !== null ? { collection_strategies: collectionStrategies } : {}),
        });
        renderProvidersSection(pane);
    } catch (err) {
        if (btn) { btn.textContent = 'Save'; btn.disabled = false; }
        alert(`Save failed: ${err.message}`);
    }
}

async function renderTokensSection(pane) {
    try {
        const health = await fetchTokenHealth();
        pane.innerHTML = `<h3 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide mb-4">Token Health</h3>${buildTokenHealthPanel(health.tokens)}`;
    } catch (err) {
        pane.innerHTML = `<p class="text-red-400 text-sm">Failed to load token health: ${escapeHTML(err.message)}</p>`;
    }
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

async function renderWebhooksSection(pane) {
    let webhooks = [];
    try {
        const res = await fetch('/api/v1/system/webhooks');
        webhooks = (await res.json()).webhooks || [];
    } catch (e) { /* ignore */ }

    pane.innerHTML = `<div>
        <div class="flex items-center justify-between mb-4">
            <h3 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide">Webhook Alerts</h3>
            <button onclick="addWebhookRow()" class="toggle-btn text-xs">+ Add</button>
        </div>
        <div id="webhook-rows" class="space-y-3">${webhooks.map(w => webhookRowHtml(w)).join('')}</div>
    </div>`;
}

function webhookRowHtml(w) {
    return `<div class="flex flex-wrap gap-2 items-center p-3 bg-zinc-900/50 rounded-xl" data-webhook-id="${w.id}">
        <input type="text" value="${escapeHTMLAttr(w.provider_id)}" placeholder="provider or *" class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 w-24 text-zinc-200" onchange="patchWebhook(${w.id}, 'provider_id', this.value)">
        <input type="number" value="${w.threshold_pct}" min="1" max="100" step="1" class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 w-16 text-zinc-200" onchange="patchWebhook(${w.id}, 'threshold_pct', parseFloat(this.value))">
        <span class="text-zinc-600 text-xs">%</span>
        <select class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-zinc-200" onchange="patchWebhook(${w.id}, 'channel', this.value)">
            <option value="discord" ${w.channel === 'discord' ? 'selected' : ''}>Discord</option>
            <option value="slack" ${w.channel === 'slack' ? 'selected' : ''}>Slack</option>
        </select>
        <input type="url" value="${escapeHTMLAttr(w.url)}" placeholder="Webhook URL" class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 flex-1 min-w-[180px] text-zinc-200" onchange="patchWebhook(${w.id}, 'url', this.value)">
        <button onclick="testWebhook(${w.id})" class="toggle-btn text-xs">Test</button>
        <button onclick="deleteWebhook(${w.id})" class="toggle-btn text-xs text-red-400">✕</button>
    </div>`;
}

export async function addWebhookRow() {
    const res = await fetch('/api/v1/system/webhooks', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider_id: '*', threshold_pct: 90, url: '', channel: 'discord'}),
    });
    if (res.ok) {
        const pane = document.getElementById('settings-pane');
        if (pane) renderWebhooksSection(pane);
    }
}

export async function patchWebhook(id, field, value) {
    await fetch(`/api/v1/system/webhooks/${id}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({[field]: value}),
    });
}

export async function testWebhook(id) {
    const res = await fetch(`/api/v1/system/webhooks/${id}/test`, {method: 'POST'});
    const data = await res.json();
    alert(res.ok ? 'Test sent!' : `Failed: ${data.detail}`);
}

export async function deleteWebhook(id) {
    await fetch(`/api/v1/system/webhooks/${id}`, {method: 'DELETE'});
    const pane = document.getElementById('settings-pane');
    if (pane) renderWebhooksSection(pane);
}

async function renderSystemSection(pane) {
    try {
        const [s, cfg] = await Promise.all([fetchSettings(), fetchAppConfig()]);
        const browserPref = escapeHTMLAttr(cfg.browser_preference || '');
        const globalPollVal = cfg.default_poll_interval_seconds ?? '';
        const localCollectorOn = cfg.local_collector_enabled;
        const credScrapingOn = cfg.local_credential_scraping_enabled;
        const pollSelectOpts = POLL_OPTIONS.map(o => `<option value="${o.value}" ${globalPollVal === o.value ? 'selected' : ''}>${o.label}</option>`).join('');
        pane.innerHTML = `<h3 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide mb-4">System</h3>
            <div class="space-y-4">
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50"><span class="text-zinc-400 text-sm">Run Mode</span><span class="text-zinc-100 mono bg-zinc-800 px-2 py-0.5 rounded text-xs">${escapeHTML(s.run_mode)}</span></div>
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50"><span class="text-zinc-400 text-sm">Host / Port</span><span class="text-zinc-100 mono text-sm">${escapeHTML(s.app_host)}:${s.app_port}</span></div>
                <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                    <div><span class="text-zinc-400 text-sm">Local Collectors</span><p class="text-[10px] text-zinc-600 mt-0.5">Enable reading local files and DBs (CLI tools, logs)</p></div>
                    <button type="button" id="toggle-local-collector" data-enabled="${localCollectorOn}" class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${localCollectorOn ? 'bg-violet-600' : 'bg-zinc-700'}"><span class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${localCollectorOn ? 'translate-x-5' : 'translate-x-0'}"></span></button>
                </div>
                <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                    <div><span class="text-zinc-400 text-sm">Credential Scraping</span><p class="text-[10px] text-zinc-600 mt-0.5">Allow reading browser cookies and credential files</p></div>
                    <button type="button" id="toggle-cred-scraping" data-enabled="${credScrapingOn}" class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${credScrapingOn ? 'bg-violet-600' : 'bg-zinc-700'}"><span class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${credScrapingOn ? 'translate-x-5' : 'translate-x-0'}"></span></button>
                </div>
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50"><span class="text-zinc-400 text-sm">Database Encryption</span><span class="${s.encryption_enabled ? 'text-green-400' : 'text-yellow-500'} mono text-sm">${s.encryption_enabled ? '✅ Active' : '🔓 Plaintext'}</span></div>
                ${!s.encryption_enabled ? '<p class="text-[10px] text-yellow-600 italic mt-1">Set DB_ENCRYPTION_KEY env var to secure your snapshots.</p>' : ''}
                <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                    <div><span class="text-zinc-400 text-sm">Default Poll Interval</span><p class="text-[10px] text-zinc-600 mt-0.5">Applies to all providers; per-provider overrides take precedence</p></div>
                    <div class="flex gap-2 items-center"><select id="field-global-poll" class="mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-1.5 text-zinc-200 focus:outline-none focus:border-violet-500"><option value="" ${!globalPollVal ? 'selected' : ''}>Per-collector default</option>${pollSelectOpts}</select><button id="save-global-poll-btn" class="toggle-btn text-xs">Save</button></div>
                </div>
                <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                    <div><span class="text-zinc-400 text-sm">Browser Preference</span><p class="text-[10px] text-zinc-600 mt-0.5">Cookie-auth order for Claude web, ChatGPT, Ollama… (e.g. safari,chrome,firefox)</p></div>
                    <div class="flex gap-2 items-center"><input id="field-browser-pref" type="text" value="${browserPref}" class="mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-1.5 text-zinc-200 w-52 focus:outline-none focus:border-violet-500" placeholder="safari,chrome,chromium,edge,firefox"><button id="save-browser-pref-btn" class="toggle-btn text-xs">Save</button></div>
                </div>
            </div>
            <div class="mt-8 p-4 bg-blue-900/20 border border-blue-800/30 rounded-xl text-xs text-blue-300 leading-relaxed"><strong>Tip:</strong> Core configuration is still managed via <code class="bg-blue-900/40 px-1 rounded">.env</code>. Provider-specific overrides can be set in the Providers section above.</div>`;

        pane.querySelector('#save-global-poll-btn')?.addEventListener('click', async function() {
            const select = pane.querySelector('#field-global-poll');
            const val = select?.value ? parseInt(select.value, 10) : 0;
            this.textContent = 'Saving…';
            this.disabled = true;
            try {
                await putAppConfig({ default_poll_interval_seconds: val });
                this.textContent = 'Saved';
                setTimeout(() => { this.textContent = 'Save'; this.disabled = false; }, 1500);
            } catch (err) {
                this.textContent = 'Error';
                this.disabled = false;
            }
        });

        pane.querySelector('#save-browser-pref-btn')?.addEventListener('click', async function() {
            const input = pane.querySelector('#field-browser-pref');
            const val = input?.value.trim() || null;
            this.textContent = 'Saving…';
            this.disabled = true;
            try {
                await putAppConfig({ browser_preference: val });
                this.textContent = 'Saved';
                setTimeout(() => { this.textContent = 'Save'; this.disabled = false; }, 1500);
            } catch (err) {
                this.textContent = 'Error';
                this.disabled = false;
            }
        });

        function wireSystemToggle(btnId, cfgKey) {
            const btn = pane.querySelector(`#${btnId}`);
            if (!btn) return;
            btn.addEventListener('click', async function() {
                const newVal = this.dataset.enabled !== 'true';
                this.dataset.enabled = newVal;
                this.classList.toggle('bg-violet-600', newVal);
                this.classList.toggle('bg-zinc-700', !newVal);
                const knob = this.querySelector('span');
                if (knob) {
                    knob.classList.toggle('translate-x-5', newVal);
                    knob.classList.toggle('translate-x-0', !newVal);
                }
                try {
                    await putAppConfig({ [cfgKey]: newVal });
                } catch (err) {
                    this.dataset.enabled = !newVal;
                    this.classList.toggle('bg-violet-600', !newVal);
                    this.classList.toggle('bg-zinc-700', newVal);
                    if (knob) {
                        knob.classList.toggle('translate-x-5', !newVal);
                        knob.classList.toggle('translate-x-0', newVal);
                    }
                }
            });
        }
        wireSystemToggle('toggle-local-collector', 'local_collector_enabled');
        wireSystemToggle('toggle-cred-scraping', 'local_credential_scraping_enabled');
    } catch (err) {
        pane.innerHTML = `<p class="text-red-400 text-sm">Failed to load system info: ${escapeHTML(err.message)}</p>`;
    }
}

export function initSettingsView() {
    // Settings view uses inline onclick handlers in HTML
    // No additional event listener setup needed
}