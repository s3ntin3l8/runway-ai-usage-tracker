import { fetchLimits, getGitHubOAuthStatus, initGitHubOAuth, pollGitHubOAuth, logoutGitHub, fetchHistory, fetchSettings, fetchFleet, patchSidecar, deleteSidecarAPI, fetchTokenHealth, postTokenRefresh, forceCollect, fetchProviderConfigs, putProviderConfig, fetchAppConfig, putAppConfig, collectProvider } from './api.js';
import { STATE, HEALTH_CONFIG } from './state.js';
import { buildCard, buildModalContent, buildGitHubOAuthModal, buildProviderSection, buildProviderSummaryCard, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildHealthBar, buildProviderModal, buildProviderSparklineStrip } from './components.js';
import { updateCharts, destroyCharts } from './charts.js';
import { loadHistoryView, initHistoryView } from './views/history.js';
import { loadSettingsView } from './views/settings.js';

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

// History tab state
const historyState = {
    days: 7,
    activeProviders: null, // null = all; Set<string> when filtering
    metric: 'percent',     // 'percent' | 'tokens' | 'cost'
};
let _historyCache = [];

// Auto-refresh timer reference
let refreshTimer = null;
let githubPollTimer = null;
let loadDataGeneration = 0; // Prevents stale fetch responses from overwriting newer data

/**
 * View Management
 */
window.switchView = function(viewId) {
    // Hide all views
    document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
    // Show selected view
    document.getElementById(`view-${viewId}`).classList.remove('hidden');
    
    // Update nav links
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    document.getElementById(`nav-${viewId}`).classList.add('active');
    
    // Load data for the view
    if (viewId === 'dashboard' && STATE.data.length === 0) loadData();
    if (viewId === 'history') loadHistoryView();
    if (viewId === 'settings') loadSettingsView();
    if (viewId === 'fleet') loadFleet();
}

window.toggleHistoryProvider = function(pid) {
    if (!historyState.activeProviders) {
        // All active → select only this one
        historyState.activeProviders = new Set([pid]);
    } else if (historyState.activeProviders.has(pid)) {
        historyState.activeProviders.delete(pid);
        if (historyState.activeProviders.size === 0) historyState.activeProviders = null;
    } else {
        historyState.activeProviders.add(pid);
    }
    updateCsvHref();
    renderHistoryFromCache();
};

window.setHistoryDays = function(days) {
    historyState.days = days;
    document.querySelectorAll('#history-range-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', parseFloat(btn.dataset.days) === days);
    });
    updateCsvHref();
    loadHistory();
};

window.setHistoryMetric = function(metric) {
    historyState.metric = metric;
    document.querySelectorAll('#history-metric-btns .toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.metric === metric);
    });
    renderHistoryFromCache();
};

function updateCsvHref() {
    const btn = document.getElementById('csv-download-btn');
    if (!btn) return;
    const params = new URLSearchParams({ format: 'csv', days: historyState.days });
    if (historyState.activeProviders && historyState.activeProviders.size === 1) {
        params.set('provider_id', [...historyState.activeProviders][0]);
    }
    btn.href = `/api/v1/usage/history?${params.toString()}`;
}

function renderHistoryFromCache() {
    const history = _historyCache;
    // Sparkline strip (all providers, shows active state)
    const stripEl = document.getElementById('history-sparkline-strip');
    if (stripEl) stripEl.innerHTML = buildProviderSparklineStrip(history, historyState.activeProviders);

    // Filter history for chart + table
    let filtered = history;
    if (historyState.activeProviders) {
        filtered = history.filter(s => historyState.activeProviders.has(s.provider_id));
    }
    // Update chart (async - lazy loads Chart.js on first call)
    updateCharts(filtered, historyState.metric);

    // Table
    const container = document.getElementById('history-content');
    if (!filtered || filtered.length === 0) {
        container.innerHTML = '<p class="text-zinc-500 italic">No history data found.</p>';
        return;
    }
    let html = `<table class="w-full text-left mono text-[11px]">
        <thead class="text-zinc-600 border-b border-zinc-800/50">
            <tr>
                <th class="py-2 px-2">Time</th>
                <th class="py-2 px-2">Provider</th>
                <th class="py-2 px-2">Service</th>
                <th class="py-2 px-2 text-right">Usage</th>
            </tr>
        </thead>
        <tbody class="text-zinc-400">`;
    filtered.slice(0, 50).forEach(s => {
        const date = new Date(s.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        const usage = s.used_value !== null ? `${s.used_value.toLocaleString()}${s.unit_type === 'percent' ? '%' : ''}` : '—';
        html += `<tr class="border-b border-zinc-900/30 hover:bg-zinc-800/10 transition-colors">
            <td class="py-2 px-2 text-zinc-600">${date}</td>
            <td class="py-2 px-2 text-zinc-500">${escapeHTML(s.provider_id || '—')}</td>
            <td class="py-2 px-2 font-medium text-zinc-300">${escapeHTML(s.service_name || '—')}</td>
            <td class="py-2 px-2 text-right font-bold text-zinc-400">${usage}</td>
        </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

async function loadHistory() {
    updateCsvHref();
    const container = document.getElementById('history-content');
    container.innerHTML = '<p class="text-zinc-500 animate-pulse">Loading history...</p>';

    try {
        const history = await fetchHistory({ days: historyState.days, limit: 500 });
        _historyCache = history || [];
        renderHistoryFromCache();
    } catch (err) {
        destroyCharts();
        container.innerHTML = `<p class="text-red-400">Failed to load history: ${escapeHTML(err.message)}</p>`;
    }
}

// --- Settings tab ---

// Module-level state for the settings section
let _settingsSelectedProvider = null;
let _settingsProviderOriginal = null; // snapshot for discard

function loadSettings() {
    const activeSection = localStorage.getItem('settings_section') || 'providers';
    // Mark the correct nav item active
    document.querySelectorAll('.settings-nav-item').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.section === activeSection);
        btn.addEventListener('click', () => switchSettingsSection(btn.dataset.section));
    });
    renderSettingsSection(activeSection);
}

window.switchSettingsSection = function(name) {
    localStorage.setItem('settings_section', name);
    document.querySelectorAll('.settings-nav-item').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.section === name);
    });
    renderSettingsSection(name);
};

function renderSettingsSection(name) {
    const pane = document.getElementById('settings-pane');
    if (!pane) return;
    pane.innerHTML = '<p class="text-zinc-500 animate-pulse text-sm">Loading…</p>';
    if (name === 'providers') renderProvidersSection(pane);
    else if (name === 'tokens') renderTokensSection(pane);
    else if (name === 'webhooks') renderWebhooksSection(pane);
    else if (name === 'system') renderSystemSection(pane);
}

// ── Providers section ─────────────────────────────────────────────────────────

const POLL_OPTIONS = [
    { label: '1 min',   value: 60 },
    { label: '5 min',   value: 300 },
    { label: '15 min',  value: 900 },
    { label: '30 min',  value: 1800 },
    { label: '1 hour',  value: 3600 },
];

async function renderProvidersSection(pane) {
    try {
        const { providers } = await fetchProviderConfigs();
        if (!_settingsSelectedProvider && providers.length > 0) {
            _settingsSelectedProvider = providers[0].provider_id;
        }
        pane.innerHTML = buildProvidersSectionHTML(providers);
        // Wire up provider list clicks
        pane.querySelectorAll('.provider-list-item').forEach(item => {
            item.addEventListener('click', () => {
                _settingsSelectedProvider = item.dataset.providerId;
                renderProvidersSection(pane);
            });
        });
        // Wire up save/discard
        const form = pane.querySelector('#provider-config-form');
        if (form) {
            const selected = providers.find(p => p.provider_id === _settingsSelectedProvider);
            _settingsProviderOriginal = selected ? { ...selected } : null;

            pane.querySelector('#provider-save-btn')?.addEventListener('click', () => saveProviderConfig(pane, form, _settingsSelectedProvider));
            pane.querySelector('#provider-discard-btn')?.addEventListener('click', () => renderProvidersSection(pane));
            pane.querySelector('#provider-raw-data-btn')?.addEventListener('click', () => viewRawProviderData(_settingsSelectedProvider));
            // Wire up enabled toggle switch
            pane.querySelector('#field-enabled-toggle')?.addEventListener('click', function() {
                const next = this.dataset.enabled !== 'true';
                this.dataset.enabled = String(next);
                this.classList.toggle('bg-violet-600', next);
                this.classList.toggle('bg-zinc-700', !next);
                this.querySelector('span').classList.toggle('translate-x-5', next);
                this.querySelector('span').classList.toggle('translate-x-0', !next);
            });
            // Wire up API key edit toggle
            pane.querySelector('#api-key-edit-btn')?.addEventListener('click', () => toggleApiKeyEdit(pane));
            // Wire up session cookie edit toggle
            pane.querySelector('#session-cookie-edit-btn')?.addEventListener('click', () => toggleSessionCookieEdit(pane));
        }
    } catch (err) {
        pane.innerHTML = `<p class="text-red-400 text-sm">Failed to load providers: ${escapeHTML(err.message)}</p>`;
    }
}

function buildProvidersSectionHTML(providers) {
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

    return `
        <div class="flex gap-4 h-full">
            <!-- Provider list -->
            <div class="w-36 shrink-0 flex flex-col gap-0.5 overflow-y-auto">
                <p class="text-[10px] uppercase tracking-widest text-zinc-600 px-3 mb-1">Providers</p>
                ${listHTML}
            </div>
            <!-- Config form -->
            <div class="flex-1 min-w-0">
                ${formHTML}
            </div>
        </div>`;
}

function buildProviderForm(p) {
    const defaultTtlLabel = POLL_OPTIONS.find(o => o.value === p.default_ttl_seconds)?.label
        || `${p.default_ttl_seconds}s`;
    const pollSelectOpts = POLL_OPTIONS.map(o =>
        `<option value="${o.value}" ${p.poll_interval_seconds === o.value ? 'selected' : ''}>${o.label}</option>`
    ).join('');

    return `
        <form id="provider-config-form" class="space-y-4" onsubmit="return false">
            <h3 class="text-base font-semibold text-zinc-100 flex items-center gap-2">
                <span>${p.icon}</span> ${escapeHTML(p.name)}
            </h3>

            <!-- Enabled toggle -->
            <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                <div>
                    <span class="text-sm text-zinc-400">Enabled</span>
                    <p class="text-[10px] text-zinc-600 mt-0.5">Polling active for this provider</p>
                </div>
                <button type="button" id="field-enabled-toggle"
                    data-enabled="${p.enabled}"
                    class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${p.enabled ? 'bg-violet-600' : 'bg-zinc-700'}">
                    <span class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${p.enabled ? 'translate-x-5' : 'translate-x-0'}"></span>
                </button>
            </div>

            ${p.supports_api_key ? `
            <!-- API Key -->
            <div class="py-3 border-b border-zinc-800/50">
                <div class="flex items-center justify-between mb-2">
                    <span class="text-sm text-zinc-400">API Key</span>
                    <button type="button" id="api-key-edit-btn" class="toggle-btn text-xs">Edit</button>
                </div>
                <div id="api-key-display" class="${p.api_key_set ? '' : 'hidden'}">
                    <span class="mono text-xs text-zinc-500">••••••••••••••••</span>
                </div>
                <div id="api-key-input-row" class="${p.api_key_set ? 'hidden' : ''}">
                    <input type="text" id="field-api-key" placeholder="${p.api_key_set ? 'Leave blank to keep current key' : 'Enter API key'}"
                        class="w-full mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-zinc-200 focus:outline-none focus:border-violet-500">
                </div>
                ${!p.api_key_set ? '<p class="text-[10px] text-zinc-600 mt-1">No key stored — env var / file / keychain used as fallback.</p>' : ''}
            </div>
            ` : ''}

            ${p.supports_session_cookie ? `
            <!-- Session Cookie -->
            <div class="py-3 border-b border-zinc-800/50">
                <div class="flex items-center justify-between mb-2">
                    <div>
                        <span class="text-sm text-zinc-400">Session Cookie</span>
                        <p class="text-[10px] text-zinc-600 mt-0.5">Manual override — bypasses browser cookie extraction</p>
                    </div>
                    <button type="button" id="session-cookie-edit-btn" class="toggle-btn text-xs">Edit</button>
                </div>
                <div id="session-cookie-display" class="${p.session_cookie_set ? '' : 'hidden'}">
                    <span class="mono text-xs text-zinc-500">••••••••••••••••</span>
                </div>
                <div id="session-cookie-input-row" class="${p.session_cookie_set ? 'hidden' : ''}">
                    <input type="text" id="field-session-cookie" placeholder="${p.session_cookie_set ? 'Leave blank to keep current value' : 'Paste session cookie value'}"
                        class="w-full mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-zinc-200 focus:outline-none focus:border-violet-500">
                </div>
                ${!p.session_cookie_set ? '<p class="text-[10px] text-zinc-600 mt-1">No cookie stored — browser extraction used as fallback.</p>' : ''}
            </div>
            ` : ''}

            ${p.provider_id === 'github' ? `
            <!-- GitHub OAuth -->
            <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                <div>
                    <span class="text-sm text-zinc-400">GitHub OAuth</span>
                    <p class="text-[10px] text-zinc-600 mt-0.5">${STATE.githubAuth?.authenticated ? `Connected as <span class="text-zinc-400">${escapeHTML(STATE.githubAuth.account?.login || '')}</span>` : 'Not connected'}</p>
                </div>
                ${STATE.githubAuth?.authenticated
                    ? `<button type="button" onclick="handleGitHubLogout()" class="toggle-btn text-xs text-red-400" style="border-color:#f87171">Disconnect</button>`
                    : `<button type="button" onclick="startGitHubLogin()" class="toggle-btn text-xs">Connect</button>`
                }
            </div>
            ` : ''}

            <!-- Account Label -->
            <label class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                <span class="text-sm text-zinc-400">Account Label</span>
                <input type="text" id="field-account-label" value="${escapeHTMLAttr(p.account_label || '')}"
                    placeholder="Auto-detected"
                    class="mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-1.5 text-zinc-200 w-48 focus:outline-none focus:border-violet-500">
            </label>

            <!-- Poll Interval -->
            <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                <span class="text-sm text-zinc-400">Poll Interval Override</span>
                <select id="field-poll-interval" class="mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-1.5 text-zinc-200 focus:outline-none focus:border-violet-500">
                    <option value="" ${!p.poll_interval_seconds ? 'selected' : ''}>Default (${defaultTtlLabel})</option>
                    ${pollSelectOpts}
                </select>
            </div>

            <!-- Actions -->
            <div class="flex justify-between items-center pt-2">
                <button type="button" id="provider-raw-data-btn" class="toggle-btn text-xs" style="border-color:#3f3f46;color:#a1a1aa;">View Raw Data</button>
                <div class="flex gap-2">
                    <button type="button" id="provider-discard-btn" class="toggle-btn text-xs">Discard</button>
                    <button type="button" id="provider-save-btn" class="toggle-btn text-xs" style="border-color:#7c3aed;color:#c4b5fd;">Save</button>
                </div>
            </div>
        </form>`;
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
    const accountLabel = form.querySelector('#field-account-label')?.value || null;

    // Only include api_key if the edit row is currently visible (user explicitly opened it)
    const apiKeyInputRow = pane.querySelector('#api-key-input-row');
    const apiKeyVisible = apiKeyInputRow && !apiKeyInputRow.classList.contains('hidden');
    const apiKey = apiKeyVisible ? (form.querySelector('#field-api-key')?.value ?? '') : undefined;

    // Only include session_cookie if the edit row is currently visible (user explicitly opened it)
    const sessionCookieInputRow = pane.querySelector('#session-cookie-input-row');
    const sessionCookieVisible = sessionCookieInputRow && !sessionCookieInputRow.classList.contains('hidden');
    const sessionCookie = sessionCookieVisible ? (form.querySelector('#field-session-cookie')?.value ?? '') : undefined;

    // Send 0 as sentinel for "use default" so the backend clears any stored override
    const pollRaw = form.querySelector('#field-poll-interval')?.value;
    const pollIntervalSeconds = pollRaw ? parseInt(pollRaw, 10) : 0;

    try {
        await putProviderConfig(providerId, {
            enabled,
            ...(apiKey !== undefined ? { api_key: apiKey } : {}),
            ...(sessionCookie !== undefined ? { session_cookie: sessionCookie } : {}),
            account_label: accountLabel,
            poll_interval_seconds: pollIntervalSeconds,
        });
        renderProvidersSection(pane);
    } catch (err) {
        if (btn) { btn.textContent = 'Save'; btn.disabled = false; }
        alert(`Save failed: ${err.message}`);
    }
}

// ── Tokens section ────────────────────────────────────────────────────────────

async function renderTokensSection(pane) {
    try {
        const health = await fetchTokenHealth();
        pane.innerHTML = `
            <h3 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide mb-4">Token Health</h3>
            ${buildTokenHealthPanel(health.tokens)}`;
    } catch (err) {
        pane.innerHTML = `<p class="text-red-400 text-sm">Failed to load token health: ${escapeHTML(err.message)}</p>`;
    }
}

window.refreshToken = async function(provider, accountId) {
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
};

// ── Webhooks section ──────────────────────────────────────────────────────────

async function renderWebhooksSection(pane) {
    let webhooks = [];
    try {
        const res = await fetch('/api/v1/system/webhooks');
        webhooks = (await res.json()).webhooks || [];
    } catch (e) { /* ignore */ }

    pane.innerHTML = `
        <div>
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide">Webhook Alerts</h3>
                <button onclick="addWebhookRow()" class="toggle-btn text-xs">+ Add</button>
            </div>
            <div id="webhook-rows" class="space-y-3">
                ${webhooks.map(w => webhookRowHtml(w)).join('')}
            </div>
        </div>`;
}

function webhookRowHtml(w) {
    return `
        <div class="flex flex-wrap gap-2 items-center p-3 bg-zinc-900/50 rounded-xl" data-webhook-id="${w.id}">
            <input type="text" value="${escapeHTMLAttr(w.provider_id)}" placeholder="provider or *"
                   class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 w-24 text-zinc-200"
                   onchange="patchWebhook(${w.id}, 'provider_id', this.value)">
            <input type="number" value="${w.threshold_pct}" min="1" max="100" step="1"
                   class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 w-16 text-zinc-200"
                   onchange="patchWebhook(${w.id}, 'threshold_pct', parseFloat(this.value))">
            <span class="text-zinc-600 text-xs">%</span>
            <select class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-zinc-200"
                    onchange="patchWebhook(${w.id}, 'channel', this.value)">
                <option value="discord" ${w.channel === 'discord' ? 'selected' : ''}>Discord</option>
                <option value="slack" ${w.channel === 'slack' ? 'selected' : ''}>Slack</option>
            </select>
            <input type="url" value="${escapeHTMLAttr(w.url)}" placeholder="Webhook URL"
                   class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 flex-1 min-w-[180px] text-zinc-200"
                   onchange="patchWebhook(${w.id}, 'url', this.value)">
            <button onclick="testWebhook(${w.id})" class="toggle-btn text-xs">Test</button>
            <button onclick="deleteWebhook(${w.id})" class="toggle-btn text-xs text-red-400">✕</button>
        </div>`;
}

window.addWebhookRow = async function() {
    const res = await fetch('/api/v1/system/webhooks', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider_id: '*', threshold_pct: 90, url: '', channel: 'discord'}),
    });
    if (res.ok) {
        const pane = document.getElementById('settings-pane');
        if (pane) renderWebhooksSection(pane);
    }
};

window.patchWebhook = async function(id, field, value) {
    await fetch(`/api/v1/system/webhooks/${id}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({[field]: value}),
    });
};

window.testWebhook = async function(id) {
    const res = await fetch(`/api/v1/system/webhooks/${id}/test`, {method: 'POST'});
    const data = await res.json();
    alert(res.ok ? 'Test sent!' : `Failed: ${data.detail}`);
};

window.deleteWebhook = async function(id) {
    await fetch(`/api/v1/system/webhooks/${id}`, {method: 'DELETE'});
    const pane = document.getElementById('settings-pane');
    if (pane) renderWebhooksSection(pane);
};

// ── System section ────────────────────────────────────────────────────────────

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
        pane.innerHTML = `
            <h3 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide mb-4">System</h3>
            <div class="space-y-4">
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50">
                    <span class="text-zinc-400 text-sm">Run Mode</span>
                    <span class="text-zinc-100 mono bg-zinc-800 px-2 py-0.5 rounded text-xs">${escapeHTML(s.run_mode)}</span>
                </div>
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50">
                    <span class="text-zinc-400 text-sm">Host / Port</span>
                    <span class="text-zinc-100 mono text-sm">${escapeHTML(s.app_host)}:${s.app_port}</span>
                </div>
                <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                    <div>
                        <span class="text-zinc-400 text-sm">Local Collectors</span>
                        <p class="text-[10px] text-zinc-600 mt-0.5">Enable reading local files and DBs (CLI tools, logs)</p>
                    </div>
                    <button type="button" id="toggle-local-collector"
                        data-enabled="${localCollectorOn}"
                        class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${localCollectorOn ? 'bg-violet-600' : 'bg-zinc-700'}">
                        <span class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${localCollectorOn ? 'translate-x-5' : 'translate-x-0'}"></span>
                    </button>
                </div>
                <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                    <div>
                        <span class="text-zinc-400 text-sm">Credential Scraping</span>
                        <p class="text-[10px] text-zinc-600 mt-0.5">Allow reading browser cookies and credential files</p>
                    </div>
                    <button type="button" id="toggle-cred-scraping"
                        data-enabled="${credScrapingOn}"
                        class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${credScrapingOn ? 'bg-violet-600' : 'bg-zinc-700'}">
                        <span class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${credScrapingOn ? 'translate-x-5' : 'translate-x-0'}"></span>
                    </button>
                </div>
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50">
                    <span class="text-zinc-400 text-sm">Database Encryption</span>
                    <span class="${s.encryption_enabled ? 'text-green-400' : 'text-yellow-500'} mono text-sm">${s.encryption_enabled ? '✅ Active' : '🔓 Plaintext'}</span>
                </div>
                ${!s.encryption_enabled ? '<p class="text-[10px] text-yellow-600 italic mt-1">Set DB_ENCRYPTION_KEY env var to secure your snapshots.</p>' : ''}
                <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                    <div>
                        <span class="text-zinc-400 text-sm">Default Poll Interval</span>
                        <p class="text-[10px] text-zinc-600 mt-0.5">Applies to all providers; per-provider overrides take precedence</p>
                    </div>
                    <div class="flex gap-2 items-center">
                        <select id="field-global-poll" class="mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-1.5 text-zinc-200 focus:outline-none focus:border-violet-500">
                            <option value="" ${!globalPollVal ? 'selected' : ''}>Per-collector default</option>
                            ${pollSelectOpts}
                        </select>
                        <button id="save-global-poll-btn" class="toggle-btn text-xs">Save</button>
                    </div>
                </div>
                <div class="flex items-center justify-between py-3 border-b border-zinc-800/50">
                    <div>
                        <span class="text-zinc-400 text-sm">Browser Preference</span>
                        <p class="text-[10px] text-zinc-600 mt-0.5">Cookie-auth order for Claude web, ChatGPT, Ollama… (e.g. safari,chrome,firefox)</p>
                    </div>
                    <div class="flex gap-2 items-center">
                        <input id="field-browser-pref" type="text" value="${browserPref}"
                            class="mono text-xs bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-1.5 text-zinc-200 w-52 focus:outline-none focus:border-violet-500"
                            placeholder="safari,chrome,chromium,edge,firefox">
                        <button id="save-browser-pref-btn" class="toggle-btn text-xs">Save</button>
                    </div>
                </div>
            </div>
            <div class="mt-8 p-4 bg-blue-900/20 border border-blue-800/30 rounded-xl text-xs text-blue-300 leading-relaxed">
                <strong>Tip:</strong> Core configuration is still managed via <code class="bg-blue-900/40 px-1 rounded">.env</code>. Provider-specific overrides can be set in the Providers section above.
            </div>`;

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
                    // Revert on failure
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

async function loadFleet() {
    const container = document.getElementById('fleet-content');
    if (!container) return;
    container.innerHTML = '<p class="text-zinc-500 animate-pulse">Loading fleet...</p>';
    try {
        const data = await fetchFleet();
        container.innerHTML = buildFleetView(data.sidecars);
    } catch (err) {
        container.innerHTML = `<p class="text-red-400">Failed to load fleet: ${escapeHTML(err.message)}</p>`;
    }
}

window.editSidecarName = async function(sidecarId) {
    const newName = prompt('Enter a custom name for this sidecar:', '');
    if (newName === null) return; // cancelled
    try {
        await patchSidecar(sidecarId, { custom_name: newName.trim() || null });
        loadFleet();
    } catch (err) {
        alert('Failed to rename: ' + err.message);
    }
};

window.addSidecarTag = async function(sidecarId) {
    const tag = prompt('Enter a tag for this sidecar:');
    if (!tag || !tag.trim()) return;
    try {
        // Fetch current tags first, then append
        const fleet = await fetchFleet();
        const sidecar = fleet.sidecars.find(s => s.sidecar_id === sidecarId);
        const tags = [...(sidecar?.tags || []), tag.trim()];
        await patchSidecar(sidecarId, { tags });
        loadFleet();
    } catch (err) {
        alert('Failed to add tag: ' + err.message);
    }
};

window.deleteSidecar = async function(sidecarId) {
    if (!confirm(`Remove sidecar "${sidecarId}" from the registry?`)) return;
    try {
        await deleteSidecarAPI(sidecarId);
        loadFleet();
    } catch (err) {
        alert('Failed to delete: ' + err.message);
    }
};

/**
 * Render quota cards to the grid
 * Builds HTML from STATE.data and populates the grid element.
 * Cards are grouped by provider_id and filtered by the active context filter.
 */
function applyFilters(data) {
    const f = STATE.activeFilter;
    return data.filter(item => {
        if (f && item[f.dimension] !== f.value) return false;
        return true;
    });
}

function renderHealthBar() {
    const el = document.getElementById('health-bar');
    if (!el) return;
    el.innerHTML = buildHealthBar(STATE.data);
}

function renderGrid() {
    const grid = document.getElementById('grid');

    const visible = applyFilters(STATE.data);

    // Group by provider_id; cards without a provider_id go to '__other__'
    const groups = new Map();
    visible.forEach(item => {
        const key = item.provider_id || '__other__';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(item);
    });

    // Sort: providers with worst health first, then alphabetically
    const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };
    const sorted = [...groups.keys()].sort((a, b) => {
        if (a === '__other__') return 1;
        if (b === '__other__') return -1;
        const aWorst = groups.get(a).reduce((m, i) => Math.max(m, HEALTH_SEVERITY[i.health] || 0), 0);
        const bWorst = groups.get(b).reduce((m, i) => Math.max(m, HEALTH_SEVERITY[i.health] || 0), 0);
        if (bWorst !== aWorst) return bWorst - aWorst;
        return a.localeCompare(b);
    });

    let html = '';
    let count = 0;
    for (const key of sorted) {
        const items = groups.get(key);
        try {
            html += buildProviderSummaryCard(key, items);
            count += items.length;
        } catch (e) {
            console.error('Failed to render provider card:', key, e);
        }
    }

    if (!html) {
        html = '<p class="text-zinc-500 text-sm text-center py-8">No cards match active filters.</p>';
    }

    // Provider cards use a responsive grid (not provider sections)
    grid.innerHTML = `<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">${html}</div>`;
    document.getElementById('footer-count').textContent = count;
}

/**
 * Open the provider drill-down modal. Renders immediately, loads sparklines async.
 * @param {string} providerId
 */
window.openProviderModal = async function(providerId) {
    const items = STATE.data.filter(d => (d.provider_id || '__other__') === providerId);
    if (!items.length) return;

    const container = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');

    // Sort items worst-first
    const HEALTH_SEVERITY_MODAL = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };
    const sorted = [...items].sort((a, b) =>
        (HEALTH_SEVERITY_MODAL[b.health] || 0) - (HEALTH_SEVERITY_MODAL[a.health] || 0)
    );

    // Render immediately without history so modal opens instantly
    content.innerHTML = buildProviderModal(providerId, sorted, []);
    container.classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('modal-backdrop').onclick = closeModal;
    document.getElementById('close-modal').onclick = closeModal;
    document.getElementById('refresh-provider-btn')?.addEventListener('click', () =>
        refreshProviderModal(providerId)
    );

    // Load sparklines in the background — re-render only if modal is still open
    try {
        const history = await fetchHistory({ provider_id: providerId, days: 7, limit: 500 });
        if (container.classList.contains('active') && content.querySelector('#close-modal')) {
            content.innerHTML = buildProviderModal(providerId, sorted, history);
            document.getElementById('close-modal').onclick = closeModal;
            document.getElementById('refresh-provider-btn')?.addEventListener('click', () =>
                refreshProviderModal(providerId)
            );
        }
    } catch (e) {
        console.warn('Could not fetch history for modal sparklines:', e.message);
    }
};

async function refreshProviderModal(providerId) {
    const btn = document.getElementById('refresh-provider-btn');
    if (btn) { btn.classList.add('animate-spin'); btn.disabled = true; }
    try {
        await collectProvider(providerId, null);
        const json = await fetchLimits();
        STATE.data = json.limits;
        renderGrid();
        // Re-open with fresh data
        await window.openProviderModal(providerId);
    } catch (err) {
        console.error('Provider modal refresh failed:', err);
    } finally {
        const b = document.getElementById('refresh-provider-btn');
        if (b) { b.classList.remove('animate-spin'); b.disabled = false; }
    }
}

function renderFilterPills() {
    const container = document.getElementById('filter-pills');
    if (!container) return;

    const dim = STATE.filterDimension;
    const WINDOW_ORDER = ['session', 'daily', 'weekly', 'biweekly', 'monthly', 'prepaid', 'unknown'];
    const rawValues = [...new Set(STATE.data.map(i => i[dim]).filter(Boolean))];
    const values = dim === 'window_type'
        ? rawValues.sort((a, b) => {
            const ai = WINDOW_ORDER.indexOf(a), bi = WINDOW_ORDER.indexOf(b);
            if (ai === -1 && bi === -1) return a.localeCompare(b);
            if (ai === -1) return 1;
            if (bi === -1) return -1;
            return ai - bi;
          })
        : rawValues.sort();
    const active = STATE.activeFilter?.value;

    const pills = [`<button class="pill${!active ? ' pill-active' : ''}" onclick="setFilter(null)">All</button>`];
    values.forEach(v => {
        pills.push(`<button class="pill${active === v ? ' pill-active' : ''}" onclick="setFilter('${escapeHTMLAttr(v)}')">${escapeHTML(v)}</button>`);
    });
    container.innerHTML = pills.join('');

    // Show "Source" dimension button only when sidecars exist
    const hasSidecars = STATE.data.some(i => i.sidecar_id);
    const sidecarBtn = document.getElementById('dim-btn-sidecar');
    if (sidecarBtn) sidecarBtn.classList.toggle('hidden', !hasSidecars);

    // Highlight active dimension button
    document.querySelectorAll('.dim-btn').forEach(btn => {
        btn.classList.toggle('dim-btn-active', btn.dataset.dim === dim);
    });
}

window.setFilter = function(value) {
    STATE.activeFilter = value ? { dimension: STATE.filterDimension, value } : null;
    localStorage.setItem('runway_active_filter', JSON.stringify(STATE.activeFilter));
    renderFilterPills();
    renderGrid();
};

window.setFilterDimension = function(dim) {
    STATE.filterDimension = dim;
    STATE.activeFilter = null;
    localStorage.setItem('runway_filter_dimension', dim);
    localStorage.removeItem('runway_active_filter');
    renderFilterPills();
    renderGrid();
};

/**
 * Toggle a configuration option in the global state
 * Updates the UI button state and optionally applies side effects (e.g., compact mode)
 * @param {string} key - Configuration key to toggle (e.g., 'compact', 'remaining')
 */
window.toggleConfig = function (key) {
    STATE[key] = !STATE[key];

    // Persist to localStorage
    const storageKey = `runway_${key}`;
    localStorage.setItem(storageKey, STATE[key]);

    const btn = document.getElementById(`toggle-${key}`);
    if (btn) {
        btn.classList.toggle('active', STATE[key]);
        // Update button text for remaining toggle
        if (key === 'remaining') {
            btn.innerHTML = STATE[key] ? '📈 % Remaining' : '📊 % Used';
        }
    }
    if (key === 'compact') {
        document.body.classList.toggle('compact-mode', STATE[key]);
    }
    renderGrid();
}

/**
 * Toggle bright/dark mode
 */
window.toggleTheme = function () {
    STATE.brightMode = !STATE.brightMode;
    localStorage.setItem('runway_bright_mode', STATE.brightMode);

    applyTheme();

    // Update button UI
    const btn = document.getElementById('toggle-theme');
    if (btn) {
        btn.innerHTML = STATE.brightMode ? '🌙' : '☀️';
        btn.title = STATE.brightMode ? 'Switch to dark mode' : 'Switch to bright mode';
    }
}

/**
 * Apply current theme to document
 */
function applyTheme() {
    if (STATE.brightMode) {
        document.body.classList.add('bright-mode');
    } else {
        document.body.classList.remove('bright-mode');
    }
}

/**
 * Initialize UI elements based on initial state
 */
function initUI() {
    ['compact', 'remaining'].forEach(key => {
        const btn = document.getElementById(`toggle-${key}`);
        if (btn) {
            btn.classList.toggle('active', STATE[key]);
            if (key === 'remaining') {
                btn.innerHTML = STATE[key] ? '📈 % Remaining' : '📊 % Used';
            }
        }
    });

    if (STATE.compact) {
        document.body.classList.add('compact-mode');
    }

    // Initialize theme
    applyTheme();
    const themeBtn = document.getElementById('toggle-theme');
    if (themeBtn) {
        themeBtn.innerHTML = STATE.brightMode ? '🌙' : '☀️';
        themeBtn.title = STATE.brightMode ? 'Switch to dark mode' : 'Switch to bright mode';
    }

    checkGitHubStatus();
}

/**
 * Check and update GitHub authentication status
 */
async function checkGitHubStatus() {
    const status = await getGitHubOAuthStatus();
    STATE.githubAuth = status;
    // Refresh provider form if GitHub is currently selected in Settings
    const pane = document.getElementById('settings-pane');
    if (pane && document.querySelector('.settings-nav-item.settings-nav-active')?.dataset.section === 'providers') {
        renderProvidersSection(pane);
    }
}

// Expose these for onclick handlers in modal
window.startGitHubLogin = startGitHubLogin;
window.handleGitHubLogout = handleGitHubLogout;

/**
 * Initiate GitHub OAuth Device Flow
 */
async function startGitHubLogin() {    const container = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');

    // Show loading modal
    content.innerHTML = buildGitHubOAuthModal(null);
    container.classList.add('active');
    document.body.style.overflow = 'hidden';

    try {
        const data = await initGitHubOAuth();
        content.innerHTML = buildGitHubOAuthModal(data);
        
        // Re-attach close/cancel listeners
        document.getElementById('close-modal').onclick = cancelGitHubLogin;
        document.getElementById('cancel-github-login').onclick = cancelGitHubLogin;

        // Start polling
        let currentInterval = data.interval;
        const expireTime = Date.now() + (data.expires_in * 1000);
        
        if (githubPollTimer) clearTimeout(githubPollTimer);
        
        const poll = async () => {
            if (Date.now() > expireTime) {
                cancelGitHubLogin();
                return;
            }

            try {
                const result = await pollGitHubOAuth(data.device_code);
                if (result.status === 'success') {
                    githubPollTimer = null;
                    closeModal();
                    await checkGitHubStatus();
                    loadData(); // Refresh to show new GitHub limits
                    return;
                } else if (result.status === 'slow_down' && result.interval) {
                    currentInterval = result.interval;
                }
            } catch (err) {
                console.error("Polling error:", err);
            }
            
            githubPollTimer = setTimeout(poll, currentInterval * 1000);
        };
        
        githubPollTimer = setTimeout(poll, currentInterval * 1000);

    } catch (err) {
        content.innerHTML = buildGitHubOAuthModal(null, err.message);
        document.getElementById('close-modal').onclick = closeModal;
    }
}

function cancelGitHubLogin() {
    if (githubPollTimer) {
        clearTimeout(githubPollTimer);
        githubPollTimer = null;
    }
    closeModal();
}

async function handleGitHubLogout() {
    if (confirm('Disconnect GitHub account?')) {
        await logoutGitHub();
        await checkGitHubStatus();
        loadData();

        // If modal is open for a GitHub service, refresh it
        const content = document.getElementById('modal-content');
        const container = document.getElementById('modal-container');
        if (container.classList.contains('active')) {
            // Find which service was being shown
            const titleElement = content.querySelector('h2');
            if (titleElement) {
                const serviceName = titleElement.textContent;
                const item = STATE.data.find(d => d.service_name === serviceName);
                if (item && (serviceName.toLowerCase().includes('github') || serviceName.toLowerCase().includes('copilot'))) {
                    content.innerHTML = buildModalContent(item);
                    document.getElementById('close-modal').onclick = closeModal;
                }
            }
        }
    }
}

/**
 * Load quota data from the API and render the grid
 * Handles loading states, error display, and timestamp updates
 * Gracefully degrades if the API fails with detailed error messaging
 * @async
 */
async function loadData() {
    const myGeneration = ++loadDataGeneration;

    const grid = document.getElementById('grid');
    const loading = document.getElementById('loading');
    const errorBanner = document.getElementById('error-banner');
    const lastUpdated = document.getElementById('last-updated');

    grid.innerHTML = '';
    grid.classList.add('hidden');
    loading.classList.remove('hidden');
    errorBanner.classList.add('hidden');

    try {
        const json = await fetchLimits();
        if (myGeneration !== loadDataGeneration) return; // discard stale response
        STATE.data = json.limits;
        renderFilterPills();
        renderGrid();
        renderHealthBar();

        const now = new Date();
        lastUpdated.textContent = `Updated ${now.toLocaleTimeString()}`;
        lastUpdated.classList.remove('hidden');

    } catch (err) {
        if (myGeneration !== loadDataGeneration) return; // discard stale error
        console.error('Failed to fetch limits:', err);

        // Extract error message and categorize the error type
        const errorMsg = err.message || 'Unknown error occurred';
        const errorType = getErrorType(err);

        // Display user-friendly error message with technical details
        const displayMsg = `⚠ ${errorMsg}`;
        errorBanner.textContent = displayMsg;
        errorBanner.title = `Error type: ${errorType}\nFull error: ${err.toString()}`;
        errorBanner.classList.remove('hidden');

        // Log detailed error for debugging
        console.debug(`Error type detected: ${errorType}`);
        if (err instanceof TypeError) {
            console.debug('Likely network issue (CORS, no internet, etc.)');
        } else if (err instanceof SyntaxError) {
            console.debug('Invalid response format from server');
        }
    } finally {
        loading.classList.add('hidden');
        grid.classList.remove('hidden');
    }
}

/**
 * Categorize error types for better debugging
 * @param {Error} err - The error to categorize
 * @returns {string} Error category (network, server, format, unknown)
 */
function getErrorType(err) {
    if (err instanceof TypeError) return 'network';
    if (err instanceof SyntaxError) return 'format';
    if (err.message?.includes('HTTP')) return 'server';
    return 'unknown';
}

/**
 * Open the detail modal for a specific service
 * @param {string} serviceName - Name of the service to show
 */
function openModal(serviceName) {
    const item = STATE.data.find(d => d.service_name === serviceName);
    if (!item) return;

    const container = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');

    content.innerHTML = buildModalContent(item);
    container.classList.add('active');
    document.body.style.overflow = 'hidden';

    document.getElementById('close-modal').onclick = closeModal;
    document.getElementById('refresh-provider-btn')?.addEventListener('click', () =>
        refreshModalProvider(item.provider_id, item.account_id, item.service_name)
    );
}

async function refreshModalProvider(providerId, accountId, serviceName) {
    const btn = document.getElementById('refresh-provider-btn');
    if (btn) { btn.classList.add('animate-spin'); btn.disabled = true; }
    try {
        await collectProvider(providerId, accountId);
        // Reload global state then re-render modal with fresh data
        const json = await fetchLimits();
        STATE.data = json.limits;
        renderGrid();
        const fresh = STATE.data.find(d => d.service_name === serviceName);
        if (fresh) {
            document.getElementById('modal-content').innerHTML = buildModalContent(fresh);
            document.getElementById('close-modal').onclick = closeModal;
            document.getElementById('refresh-provider-btn')?.addEventListener('click', () =>
                refreshModalProvider(fresh.provider_id, fresh.account_id, fresh.service_name)
            );
        }
    } catch (err) {
        console.error('Provider refresh failed:', err);
    } finally {
        if (btn) { btn.classList.remove('animate-spin'); btn.disabled = false; }
    }
}

/**
 * Close the detail modal
 */
function closeModal() {
    const container = document.getElementById('modal-container');
    container.classList.remove('active');
    document.body.style.overflow = '';
}

window.forceRefresh = async function() {
    const btn = document.getElementById('refresh-btn');
    const icon = document.getElementById('refresh-icon');
    if (btn) btn.disabled = true;
    if (icon) icon.style.animation = 'spin 1s linear infinite';
    try {
        await forceCollect();
    } catch (e) {
        console.warn('Force collect error (server may be restarting):', e.message);
    }
    await loadData();
    if (btn) btn.disabled = false;
    if (icon) icon.style.animation = '';
};

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (refreshTimer) {
        clearInterval(refreshTimer);
    }
});

// Initial load
document.addEventListener('DOMContentLoaded', () => {
    const grid = document.getElementById('grid');
    const modalBackdrop = document.getElementById('modal-backdrop');

    // Grid click delegation for cards
    grid.addEventListener('click', (e) => {
        const card = e.target.closest('.glass-panel');
        if (card && card.dataset.service) {
            openModal(card.dataset.service);
        }
    });

    // Modal close listeners
    modalBackdrop.addEventListener('click', closeModal);
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeModal();
    });

    initUI();
    loadData();
    // Auto-refresh every 5 minutes so the UI stays current even when the poller is dormant
    refreshTimer = setInterval(() => loadData(), 5 * 60 * 1000);
});

window.handleResetProvider = async function(provider, accountId) {
    const ev = window.event;
    const btn = ev ? ev.target : null;
    const originalText = btn ? btn.innerText : 'RETRY';
    if (btn) {
        btn.disabled = true;
        btn.innerText = 'RESETTING...';
    }
    
    try {
        const query = accountId && accountId !== 'default' ? `?account_id=${accountId}` : '';
        const resp = await fetch(`/api/v1/usage/reset/${provider}${query}`, { method: 'POST' });
        if (!resp.ok) throw new Error('Reset failed');
        
        if (btn) btn.innerText = 'SUCCESS!';
        setTimeout(() => {
            const modalContainer = document.getElementById('modal-container');
            modalContainer.classList.remove('active');
            loadData();
        }, 1000);
    } catch (err) {
        if (btn) {
            btn.innerText = 'ERROR';
            btn.classList.add('bg-red-500');
        }
        alert('Failed to reset provider: ' + err.message);
        setTimeout(() => {
            if (btn) {
                btn.disabled = false;
                btn.innerText = originalText;
                btn.classList.remove('bg-red-500');
            }
        }, 2000);
    }
}

window.viewRawProviderData = async function(providerId) {
    const modal = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');
    if (!modal || !content) return;

    content.innerHTML = `
        <div class="p-12 text-center">
            <div class="inline-block w-8 h-8 border-4 border-violet-500/30 border-t-violet-500 rounded-full animate-spin mb-4"></div>
            <p class="text-zinc-500 font-bold tracking-widest text-xs uppercase">Fetching raw API data from ${escapeHTML(providerId)}...</p>
            <p class="text-[10px] text-zinc-600 mt-2">This may take up to 30 seconds if it triggers a fresh collection cycle.</p>
        </div>
    `;
    modal.classList.add('active');

    try {
        const resp = await fetch(`/api/v1/system/debug/raw/${providerId}`);
        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || 'Failed to fetch raw data');
        }
        const data = await resp.json();
        
        content.innerHTML = `
            <div class="flex justify-between items-start mb-5 pb-4 border-b border-zinc-800/50">
                <div>
                    <div class="text-xl font-black text-zinc-100 uppercase tracking-tight">Raw Data: ${escapeHTML(providerId)}</div>
                    <div class="text-[10px] text-zinc-500 mono mt-1">Provider-specific HTTP interception bundle</div>
                </div>
                <button onclick="document.getElementById('modal-container').classList.remove('active')" class="text-zinc-400 hover:text-zinc-200 transition-colors text-xl leading-none mt-0.5 w-8 h-8 flex items-center justify-center rounded-full hover:bg-zinc-800">✕</button>
            </div>
            <div class="max-h-[70vh] overflow-y-auto space-y-6 pr-2">
                ${Object.keys(data.responses).length === 0 ? `
                    <div class="bg-zinc-900/50 rounded-xl p-8 text-center border border-dashed border-zinc-800">
                        <p class="text-zinc-500 text-sm italic">No HTTP requests were captured during the collection cycle.</p>
                        <p class="text-[10px] text-zinc-600 mt-2">This usually means the data was served from the local cache.</p>
                    </div>
                ` : Object.entries(data.responses).map(([url, res]) => `
                    <div class="space-y-2">
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="px-2 py-0.5 rounded bg-zinc-800 text-zinc-400 mono text-[10px] font-bold border border-zinc-700/50">${res.status}</span>
                            <span class="text-[10px] text-zinc-500 mono truncate max-w-md">${escapeHTML(url)}</span>
                        </div>
                        <div class="bg-black/40 rounded-xl p-4 border border-zinc-800/60">
                            <pre class="text-[11px] text-zinc-300 mono whitespace-pre-wrap overflow-x-auto leading-relaxed max-h-[400px]">${escapeHTML(JSON.stringify(res.body, null, 2))}</pre>
                        </div>
                    </div>
                `).join('')}
            </div>
            <div class="mt-6 flex justify-end">
                <button onclick="document.getElementById('modal-container').classList.remove('active')" class="px-6 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 font-bold rounded-xl transition-all text-xs uppercase tracking-widest">CLOSE</button>
            </div>
        `;
    } catch (err) {
        content.innerHTML = `
            <div class="p-8 text-center">
                <div class="w-16 h-16 bg-red-500/10 text-red-500 rounded-full flex items-center justify-center mx-auto mb-4">
                    <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>
                </div>
                <h2 class="text-xl font-black text-zinc-50 mb-2">Debug Fetch Failed</h2>
                <p class="text-zinc-400 text-sm mb-6">${escapeHTML(err.message)}</p>
                <button onclick="document.getElementById('modal-container').classList.remove('active')" class="px-8 py-3 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 font-bold rounded-xl transition-all text-xs uppercase tracking-widest">DISMISS</button>
            </div>
        `;
    }
};

// Expose functions needed by inline onclick handlers in HTML
window.switchView = switchView;

