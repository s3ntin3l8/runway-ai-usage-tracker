import { fetchLimits, getGitHubOAuthStatus, initGitHubOAuth, pollGitHubOAuth, logoutGitHub, fetchHistory, fetchSettings, fetchFleet, patchSidecar, deleteSidecarAPI, fetchTokenHealth, postTokenRefresh } from './api.js';
import { STATE, HEALTH_CONFIG, REFRESH_CONFIG } from './state.js';
import { buildCard, buildModalContent, buildGitHubOAuthModal, buildProviderSection, buildProviderSummaryCard, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildHealthBar } from './components.js';
import { updateCharts, setChartView as _setChartView, destroyCharts } from './charts.js';

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

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
    if (viewId === 'history') loadHistory();
    if (viewId === 'settings') loadSettings();
    if (viewId === 'fleet') loadFleet();
}

async function loadHistory() {
    const container = document.getElementById('history-content');
    container.innerHTML = '<p class="text-zinc-500 animate-pulse">Loading history...</p>';
    
    try {
        const history = await fetchHistory();
        if (!history || history.length === 0) {
            container.innerHTML = '<p class="text-zinc-500 italic">No history snapshots found yet.</p>';
            return;
        }
        
        let html = `
            <table class="w-full text-left mono text-[11px]">
                <thead class="text-zinc-600 border-b border-zinc-800/50">
                    <tr>
                        <th class="py-2 px-2">Time (UTC)</th>
                        <th class="py-2 px-2">Service</th>
                        <th class="py-2 px-2 text-right">Usage</th>
                    </tr>
                </thead>
                <tbody class="text-zinc-400">
        `;
        
        history.slice(0, 50).forEach(s => {
            const date = new Date(s.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
            const usage = s.used_value !== null ? `${s.used_value.toLocaleString()}${s.unit_type === 'percent' ? '%' : ''}` : '—';
            html += `
                <tr class="border-b border-zinc-900/30 hover:bg-zinc-800/10 transition-colors">
                    <td class="py-2 px-2 text-zinc-600">${date}</td>
                    <td class="py-2 px-2 font-medium text-zinc-300">${s.service_name}</td>
                    <td class="py-2 px-2 text-right font-bold text-zinc-400">${usage}</td>
                </tr>
            `;
        });
        
        html += '</tbody></table>';
        container.innerHTML = html;
        updateCharts(history);
    } catch (err) {
        destroyCharts();
        container.innerHTML = `<p class="text-red-400">Failed to load history: ${err.message}</p>`;
    }
}

async function loadSettings() {
    const container = document.getElementById('settings-content');
    container.innerHTML = '<p class="text-zinc-500 animate-pulse">Loading settings...</p>';
    
    try {
        const s = await fetchSettings();
        container.innerHTML = `
            <div class="space-y-4">
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50">
                    <span class="text-zinc-400">Run Mode</span>
                    <span class="text-zinc-100 mono bg-zinc-800 px-2 py-0.5 rounded text-xs">${s.run_mode}</span>
                </div>
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50">
                    <span class="text-zinc-400">Host / Port</span>
                    <span class="text-zinc-100 mono text-sm">${s.app_host}:${s.app_port}</span>
                </div>
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50">
                    <span class="text-zinc-400">Local Collectors</span>
                    <span class="${s.local_collector_enabled ? 'text-green-400' : 'text-zinc-500'} mono text-sm">${s.local_collector_enabled ? 'Enabled' : 'Disabled'}</span>
                </div>
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50">
                    <span class="text-zinc-400">Credential Scraping</span>
                    <span class="${s.local_credential_scraping ? 'text-green-400' : 'text-zinc-500'} mono text-sm">${s.local_credential_scraping ? 'Enabled' : 'Disabled'}</span>
                </div>
                <div class="flex justify-between items-center py-3 border-b border-zinc-800/50">
                    <span class="text-zinc-400">Database Encryption</span>
                    <span class="${s.encryption_enabled ? 'text-green-400' : 'text-yellow-500'} mono text-sm">${s.encryption_enabled ? '✅ Active' : '🔓 Plaintext'}</span>
                </div>
                ${!s.encryption_enabled ? '<p class="text-[10px] text-yellow-600 italic">Set DB_ENCRYPTION_KEY env var to secure your snapshots.</p>' : ''}
            </div>
            
            <div class="mt-8 p-4 bg-blue-900/20 border border-blue-800/30 rounded-xl text-xs text-blue-300 leading-relaxed">
                <strong>Tip:</strong> You can still use <code class="bg-blue-900/40 px-1 rounded">.env</code> for core configuration. This UI will eventually allow real-time changes.
            </div>
        `;
        // Append token health panel
        try {
            const health = await fetchTokenHealth();
            const extra = document.getElementById('settings-extra');
            if (extra) extra.innerHTML = buildTokenHealthPanel(health.tokens);
        } catch (err) {
            // Non-critical — silently skip if token health unavailable
            console.warn('Token health unavailable:', err.message);
        }
        // Append webhook alerts section
        await renderWebhookSettings();
    } catch (err) {
        container.innerHTML = `<p class="text-red-400">Failed to load settings: ${err.message}</p>`;
    }
}

window.refreshToken = async function(provider, accountId) {
    try {
        const d = await postTokenRefresh(provider, accountId);
        if (d.status === 'refreshed') loadSettings();
        else alert('Refresh reported non-success: ' + JSON.stringify(d));
    } catch (err) {
        alert('Token refresh failed: ' + err.message);
    }
};

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
        const isDisabled = STATE.disabledServices.includes(item.service_name);
        return !isDisabled || STATE.showHidden;
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
        const aWorst = Math.max(...(groups.get(a).map(i => HEALTH_SEVERITY[i.health] || 0)));
        const bWorst = Math.max(...(groups.get(b).map(i => HEALTH_SEVERITY[i.health] || 0)));
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
    grid.innerHTML = `<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">${html}</div>`;
    document.getElementById('footer-count').textContent = count;
}

/**
 * Open the provider drill-down modal. Fetches 7d history for sparklines.
 * Full implementation added in Task 4.
 * @param {string} providerId
 */
window.openProviderModal = async function(providerId) {
    const items = STATE.data.filter(d => (d.provider_id || '__other__') === providerId);
    if (!items.length) return;

    const container = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');

    // Show loading state immediately
    content.innerHTML = `<div class="p-8 text-center text-zinc-500 text-sm animate-pulse">Loading ${escapeHTMLAttr(providerId)}…</div>`;
    container.classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('modal-backdrop').onclick = closeModal;

    // Placeholder — full implementation in Task 4
    content.innerHTML = `<div class="p-6">
        <div class="flex justify-between items-center mb-4">
            <h2 class="text-lg font-bold text-zinc-100">${escapeHTMLAttr(providerId)}</h2>
            <button id="close-modal" onclick="closeModal()" class="text-zinc-500 hover:text-zinc-300">✕</button>
        </div>
        <p class="text-zinc-500 text-sm">${items.length} service(s) — full modal in Task 4</p>
    </div>`;
    document.getElementById('close-modal').onclick = closeModal;
};

function renderFilterPills() {
    const container = document.getElementById('filter-pills');
    if (!container) return;

    const dim = STATE.filterDimension;
    const values = [...new Set(STATE.data.map(i => i[dim]).filter(Boolean))].sort();
    const active = STATE.activeFilter?.value;

    const pills = [`<button class="pill${!active ? ' pill-active' : ''}" onclick="setFilter(null)">All</button>`];
    values.forEach(v => {
        pills.push(`<button class="pill${active === v ? ' pill-active' : ''}" onclick="setFilter('${escapeHTMLAttr(v)}')">${escapeHTML(v)}</button>`);
    });
    container.innerHTML = pills.join('');

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
    const storageKey = `runway_${key === 'showHidden' ? 'show_hidden' : key}`;
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
    ['compact', 'remaining', 'showHidden'].forEach(key => {
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
 * Toggle a service's disabled state
 * @param {string} serviceName - Name of the service to toggle
 */
window.toggleService = function (serviceName) {
    const index = STATE.disabledServices.indexOf(serviceName);
    if (index === -1) {
        STATE.disabledServices.push(serviceName);
    } else {
        STATE.disabledServices.splice(index, 1);
    }

    // Persist to localStorage
    localStorage.setItem('runway_disabled_services', JSON.stringify(STATE.disabledServices));

    // Refresh UI
    renderGrid();

    // Update modal content if it's open
    const item = STATE.data.find(d => d.service_name === serviceName);
    if (item) {
        document.getElementById('modal-content').innerHTML = buildModalContent(item);
        // Re-attach close listener
        document.getElementById('close-modal').onclick = closeModal;
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

    // Add close listener after injection
    document.getElementById('close-modal').onclick = closeModal;
}

/**
 * Close the detail modal
 */
function closeModal() {
    const container = document.getElementById('modal-container');
    container.classList.remove('active');
    document.body.style.overflow = '';
}

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

// --- Webhook alert settings ---

async function renderWebhookSettings() {
    const container = document.getElementById('settings-extra');
    if (!container) return;

    let webhooks = [];
    try {
        const res = await fetch('/api/v1/system/webhooks');
        webhooks = (await res.json()).webhooks || [];
    } catch (e) { /* ignore */ }

    container.insertAdjacentHTML('beforeend', `
        <div class="mt-8 border-t border-zinc-800 pt-6">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide">Webhook Alerts</h3>
                <button onclick="addWebhookRow()" class="toggle-btn text-xs">+ Add</button>
            </div>
            <div id="webhook-rows" class="space-y-3">
                ${webhooks.map(w => webhookRowHtml(w)).join('')}
            </div>
        </div>
    `);
}

function webhookRowHtml(w) {
    return `
        <div class="flex flex-wrap gap-2 items-center p-3 bg-zinc-900/50 rounded-xl" data-webhook-id="${w.id}">
            <input type="text" value="${w.provider_id}" placeholder="provider or *"
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
            <input type="url" value="${w.url}" placeholder="Webhook URL"
                   class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 flex-1 min-w-[180px] text-zinc-200"
                   onchange="patchWebhook(${w.id}, 'url', this.value)">
            <button onclick="testWebhook(${w.id})" class="toggle-btn text-xs">Test</button>
            <button onclick="deleteWebhook(${w.id})" class="toggle-btn text-xs text-red-400">✕</button>
        </div>
    `;
}

window.addWebhookRow = async function() {
    const res = await fetch('/api/v1/system/webhooks', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider_id: '*', threshold_pct: 90, url: '', channel: 'discord'}),
    });
    if (res.ok) loadSettings();
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
    loadSettings();
};
