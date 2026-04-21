import { fetchLimits, getGitHubOAuthStatus, initGitHubOAuth, pollGitHubOAuth, logoutGitHub, fetchHistory, fetchSettings, fetchFleet, patchSidecar, deleteSidecarAPI, fetchTokenHealth, postTokenRefresh, forceCollect, fetchProviderConfigs, putProviderConfig, fetchAppConfig, putAppConfig, collectProvider, getDashboardLayout, putDashboardLayout } from './api.js';
import { STATE, HEALTH_CONFIG } from './state.js';
import { applyOrder, cardKey, extractProviderOrder, extractCardOrder } from './layout.js';
import { ensureSortable } from './sortable.js';
import { buildCard, buildModalContent, buildGitHubOAuthModal, buildProviderSection, buildProviderSummaryCard, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildHealthBar, buildProviderModal, buildProviderSparklineStrip } from './components.js';
import { updateCharts, destroyCharts } from './charts.js';
import { loadHistoryView, initHistoryView, setHistoryDays, setHistoryMetric, toggleHistoryProvider } from './views/history.js';
import { loadSettingsView, renderProvidersSection, refreshToken, deleteToken } from './views/settings.js';
import { loadFleetView, editSidecarName, addSidecarTag, deleteSidecar, triggerSidecarCollect } from './views/fleet.js';
import { loadDashboard, initDashboardView, setFilter, setFilterDimension } from './views/dashboard.js';

// Alias for backwards compatibility
window.loadDashboard = loadDashboard;

function escapeHTML(str) {
    if (!str) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return str.replace(/[&<>"']/g, m => map[m]);
}

// History data is now fully managed via views/history.js

// Auto-refresh timer reference
let refreshTimer = null;
let githubPollTimer = null;
let loadDataGeneration = 0; // Prevents stale fetch responses from overwriting newer data

/**
 * View Management
 */
const KNOWN_VIEWS = ['dashboard', 'history', 'fleet', 'settings', 'auth', 'error'];

window.switchView = async function(viewId) {
    if (!KNOWN_VIEWS.includes(viewId)) viewId = 'dashboard';
    if (STATE.editMode) exitEditMode();
    // Hide all views
    document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
    document.getElementById(`view-${viewId}`).classList.remove('hidden');

    // Update nav links
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    document.getElementById(`nav-${viewId}`).classList.add('active');

    // Sync URL (no scroll jump)
    const target = `#${viewId}`;
    if (location.hash !== target) {
        history.replaceState(null, '', target);
    }

    if (viewId === 'dashboard' && STATE.data.length === 0) {
        await loadDashboard();
    }
    if (viewId === 'history') loadHistoryView();
    if (viewId === 'settings') loadSettingsView();
    if (viewId === 'fleet') loadFleetView();
};

// Re-exports for onclick handlers in index.html are now handled in views/history.js initHistoryView()

/**
 * Authentication Management
 */
async function checkAuth() {
    const nav = document.getElementById('main-nav');
    try {
        const settings = await fetchSettings();
        
        if (settings.is_authenticated) {
            // Authorized (local, proxy, or valid key already in localStorage)
            if (nav) {
                nav.style.display = 'flex';
                nav.classList.remove('nav-locked');
            }
            return true;
        }

        // Locked - show Auth Portal
        document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
        document.getElementById('view-auth').classList.remove('hidden');
        
        if (nav) {
            nav.style.display = 'flex';
            nav.classList.add('nav-locked');
        }
        
        // Initializing Auth Form
        const authForm = document.getElementById('auth-form');
        const authError = document.getElementById('auth-error');
        const keyInput = document.getElementById('admin-key-input');
        
        if (authForm && !authForm.dataset.initialized) {
            authForm.dataset.initialized = 'true';
            authForm.addEventListener('submit', async (e) => {
                e.preventDefault();
                const key = keyInput.value.trim();
                if (!key) return;

                localStorage.setItem('runway_admin_key', key);
                authError.classList.add('hidden');

                try {
                    const check = await fetchSettings();
                    if (check.is_authenticated) {
                        // Success! Reload to boot the app with the new session
                        location.reload();
                    } else {
                        throw new Error('Invalid key');
                    }
                } catch (err) {
                    authError.classList.remove('hidden');
                    localStorage.removeItem('runway_admin_key');
                    keyInput.value = '';
                }
            });
        }
        
        return false;
    } catch (err) {
        console.error('Auth verification failed:', err);
        // Network or server error - show Error View
        document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
        document.getElementById('view-error').classList.remove('hidden');
        const msg = document.getElementById('error-message');
        if (msg) msg.textContent = `Runway could not reach the backend server: ${err.message}`;
        
        if (nav) {
            nav.style.display = 'flex';
            nav.classList.add('nav-locked');
        }
        
        return false;
    }
}

// History loading is now handled in views/history.js

// ── App Initialization ────────────────────────────────────────────────────────


// View routing and global state initialization moved to end of file

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

    // Default sort: providers with worst health first, then alphabetically
    const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };
    const defaultSorted = [...groups.keys()].sort((a, b) => {
        if (a === '__other__') return 1;
        if (b === '__other__') return -1;
        const aWorst = groups.get(a).reduce((m, i) => Math.max(m, HEALTH_SEVERITY[i.health] || 0), 0);
        const bWorst = groups.get(b).reduce((m, i) => Math.max(m, HEALTH_SEVERITY[i.health] || 0), 0);
        if (bWorst !== aWorst) return bWorst - aWorst;
        return a.localeCompare(b);
    });

    // Apply user-defined provider order on top of the default sort
    const sorted = applyOrder(
        defaultSorted.map(pid => ({ pid })),
        x => x.pid,
        STATE.layout?.provider_order ?? []
    ).map(x => x.pid);

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
    let items = STATE.data.filter(d => (d.provider_id || '__other__') === providerId);
    items = applyOrder(items, cardKey, STATE.layout?.card_orders?.[providerId] ?? []);
    if (!items.length) return;

    const container = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');

    // Keep order already applied by applyOrder (pinned first, then unpinned).
    // For unpinned items, preserve API order; user can reorder via edit mode.
    const sorted = items;

    // Render immediately without history so modal opens instantly
    content.innerHTML = buildProviderModal(providerId, sorted, []);
    container.classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('modal-backdrop').onclick = closeModal;
    document.getElementById('close-modal').onclick = closeModal;
    document.getElementById('refresh-provider-btn')?.addEventListener('click', () =>
        refreshProviderModal(providerId)
    );
    await window.__reattachCardSortables();

    // Load sparklines in the background — re-render only if modal is still open
    try {
        const history = await fetchHistory({ provider_id: providerId, days: 7, limit: 500 });
        if (container.classList.contains('active') && content.querySelector('#close-modal')) {
            content.innerHTML = buildProviderModal(providerId, sorted, history);
            document.getElementById('close-modal').onclick = closeModal;
            document.getElementById('refresh-provider-btn')?.addEventListener('click', () =>
                refreshProviderModal(providerId)
            );
            await window.__reattachCardSortables();
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

/**
 * Toggle bright/dark mode
 */
window.toggleTheme = function () {
    STATE.brightMode = !STATE.brightMode;
    localStorage.setItem('runway_bright_mode', STATE.brightMode);

    applyTheme();
    updateThemeIcon();
}

function updateThemeIcon() {
    const sunIcon = document.getElementById('theme-icon-sun');
    const moonIcon = document.getElementById('theme-icon-moon');
    if (!sunIcon || !moonIcon) return;
    sunIcon.classList.toggle('hidden', STATE.brightMode);
    moonIcon.classList.toggle('hidden', !STATE.brightMode);
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

let _providerSortable = null;
let _cardSortables = [];

async function enterEditMode() {
    STATE.editMode = true;
    document.body.classList.add('edit-mode');
    const btn = document.getElementById('toggle-edit');
    if (btn) { btn.setAttribute('aria-pressed', 'true'); btn.title = 'Done'; }

    const Sortable = await ensureSortable();

    // Provider grid (the inner flex/grid wrapper injected by renderGrid)
    const providerGrid = document.querySelector('#grid > div');
    if (providerGrid) {
        _providerSortable = new Sortable(providerGrid, {
            animation: 150,
            draggable: '[data-provider-id]',
            onEnd: onProviderDrop,
        });
    }

    // Per-service breakdown rows inside each provider summary card on the dashboard
    document.querySelectorAll('[data-subitems-for]').forEach(container => {
        const s = new Sortable(container, {
            animation: 150,
            draggable: '[data-card-key]',
            onEnd: () => onCardDrop(container.dataset.subitemsFor, container),
        });
        _cardSortables.push(s);
    });

    // Card grids inside any currently-open modal
    document.querySelectorAll('#modal-content [data-provider-id]').forEach(section => {
        const container = section.querySelector('.grid') || section;
        const s = new Sortable(container, {
            animation: 150,
            draggable: '[data-card-key]',
            onEnd: () => onCardDrop(section.dataset.providerId, container),
        });
        _cardSortables.push(s);
    });
}

function exitEditMode() {
    STATE.editMode = false;
    document.body.classList.remove('edit-mode');
    const btn = document.getElementById('toggle-edit');
    if (btn) { btn.setAttribute('aria-pressed', 'false'); btn.title = 'Edit layout'; }

    if (_providerSortable) { _providerSortable.destroy(); _providerSortable = null; }
    _cardSortables.forEach(s => s.destroy());
    _cardSortables = [];
}

async function onProviderDrop() {
    const providerGrid = document.querySelector('#grid > div');
    const order = extractProviderOrder(providerGrid);
    STATE.layout = { ...STATE.layout, provider_order: order };
    await persistLayout();
}

async function onCardDrop(providerId, container) {
    const order = extractCardOrder(container);
    STATE.layout = {
        ...STATE.layout,
        card_orders: { ...STATE.layout.card_orders, [providerId]: order },
    };
    await persistLayout();
}

async function persistLayout() {
    localStorage.setItem('runway_layout', JSON.stringify(STATE.layout));
    try {
        await putDashboardLayout(STATE.layout);
    } catch (err) {
        console.warn('Failed to persist layout (kept in localStorage)', err);
    }
}

window.__reattachCardSortables = async function() {
    if (!STATE.editMode) return;
    const Sortable = await ensureSortable();
    document.querySelectorAll('#modal-content [data-provider-id]').forEach(section => {
        const cardContainer = section.querySelector('.grid') || section;
        const s = new Sortable(cardContainer, {
            animation: 150,
            draggable: '[data-card-key]',
            onEnd: () => onCardDrop(section.dataset.providerId, cardContainer),
        });
        _cardSortables.push(s);
    });
};


/**
 * Initialize UI elements based on initial state
 */
async function initUI() {
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
    updateThemeIcon();

    checkGitHubStatus();

    // Event delegation for navigation links
    document.querySelector('#main-nav')?.addEventListener('click', (e) => {
        const link = e.target.closest('.nav-link');
        if (link && link.id) {
            const viewId = link.id.replace('nav-', '');
            switchView(viewId);
        }
    });

    // Theme toggle
    document.getElementById('toggle-theme')?.addEventListener('click', () => toggleTheme());

    // Refresh button
    document.getElementById('refresh-btn')?.addEventListener('click', () => forceRefresh());

    // Edit-mode toggle
    document.getElementById('toggle-edit')?.addEventListener('click', () => {
        if (STATE.editMode) exitEditMode();
        else enterEditMode();
    });

    // Load persisted dashboard layout (falls back to localStorage cache on failure)
    try {
        const layout = await getDashboardLayout();
        STATE.layout = layout;
        localStorage.setItem('runway_layout', JSON.stringify(layout));
    } catch (err) {
        console.warn('Failed to fetch dashboard layout; using cached/empty', err);
    }

    // Route from URL hash (so reloads stay on the active tab)
    const initialView = (location.hash || '#dashboard').replace(/^#/, '');
    await switchView(initialView);

    window.addEventListener('hashchange', () => {
        const v = (location.hash || '#dashboard').replace(/^#/, '');
        switchView(v);
    });

    // Initialize dashboard view event listeners
    initDashboardView();
    initHistoryView();
    
    // Wake Trigger: When user brings the dashboard back into focus, nudge the poller.
    // Includes a 30s debounce to prevent spamming the wake endpoint.
    let lastWakeTime = 0;
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
            const now = Date.now();
            if (now - lastWakeTime > 30000) { 
                lastWakeTime = now;
                import('./api.js').then(m => m.postWake());
            }
        }
    });
}

/**
 * Check and update GitHub authentication status
 */
async function checkGitHubStatus() {
    const status = await getGitHubOAuthStatus();
    STATE.githubAuth = status;
    // Refresh provider form if GitHub is currently selected in Settings
    const pane = document.getElementById('settings-pane');
    if (pane && document.querySelector('.settings-nav-item.active')?.dataset.section === 'providers') {
        renderProvidersSection(pane);
    }
}

// Expose these for onclick handlers in modal
window.startGitHubLogin = startGitHubLogin;
window.handleGitHubLogout = handleGitHubLogout;

/**
 * Initiate GitHub OAuth Device Flow
 */
async function startGitHubLogin() {
    const container = document.getElementById('modal-container');
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
                    
                    // Only reload dashboard data if we are actually on the dashboard
                    // to prevent jumping/flickering in other views like Settings
                    if (document.querySelector('.nav-link.active')?.id === 'nav-dashboard') {
                        loadData();
                    }
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
        
        // Only reload dashboard data if we are actually on the dashboard
        if (document.querySelector('.nav-link.active')?.id === 'nav-dashboard') {
            loadData();
        }

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

// Alias loadData to loadDashboard for backwards compatibility
const loadData = loadDashboard;

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
    initHistoryView();
    
    // Check auth before loading dashboard data
    checkAuth().then(authorized => {
        if (authorized) {
            loadDashboard();
            // Auto-refresh every 5 minutes so the UI stays current even when the poller is dormant
            refreshTimer = setInterval(() => loadDashboard(), 5 * 60 * 1000);
        }
    });
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

window.copyToClipboard = async function(text, btn) {
    try {
        await navigator.clipboard.writeText(text);
        const original = btn.innerText;
        btn.innerText = 'COPIED!';
        btn.classList.add('text-green-400');
        setTimeout(() => {
            btn.innerText = original;
            btn.classList.remove('text-green-400');
        }, 2000);
    } catch (err) {
        console.error('Failed to copy: ', err);
    }
};

// Temporary in-memory store for raw debug data to avoid putting massive strings in DOM attributes
let RAW_DATA_CACHE = {
    full: null,
    bodies: []
};

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
        
        // Populate cache for buttons
        RAW_DATA_CACHE.full = data;
        RAW_DATA_CACHE.bodies = (data.responses || []).map(res => {
            return typeof res.body === 'string' ? res.body : JSON.stringify(res.body, null, 2);
        });
        
        const responses = data.responses || [];
        
        // 1. Build the skeleton via innerHTML
        content.innerHTML = `
            <div class="flex flex-col h-full overflow-hidden">
                <div class="flex justify-between items-start mb-5 pb-4 border-b border-zinc-800/50 shrink-0">
                    <div>
                        <div class="text-xl font-black text-zinc-100 uppercase tracking-tight">Raw Data: ${escapeHTML(providerId)}</div>
                        <div class="text-[10px] text-zinc-500 mono mt-1">Provider-specific HTTP interception bundle (DOM-Isolated)</div>
                    </div>
                    <div class="flex gap-2">
                        <button id="copy-all-raw" class="toggle-btn text-[10px] py-1 px-3">Copy All</button>
                        <button onclick="document.getElementById('modal-container').classList.remove('active')" class="text-zinc-400 hover:text-zinc-200 transition-colors text-xl leading-none w-8 h-8 flex items-center justify-center rounded-full hover:bg-zinc-800">✕</button>
                    </div>
                </div>
                <div id="raw-responses-list" class="flex-1 overflow-y-auto space-y-8 pr-2 custom-scrollbar contain-strict">
                    ${responses.length === 0 ? `
                        <div class="bg-zinc-900/50 rounded-xl p-8 text-center border border-dashed border-zinc-800">
                            <p class="text-zinc-500 text-sm italic">No HTTP requests were captured during the collection cycle.</p>
                            <p class="text-[10px] text-zinc-600 mt-2">This usually means the data was served from the local cache or an internal strategy.</p>
                        </div>
                    ` : responses.map((res, idx) => `
                        <div class="space-y-2 pb-2 border-b border-zinc-800/20 last:border-0">
                            <div class="flex items-center justify-between gap-2 flex-wrap">
                                <div class="flex items-center gap-2">
                                    <span class="px-2 py-0.5 rounded bg-zinc-800 text-zinc-400 mono text-[10px] font-bold border border-zinc-700/50">${res.status}</span>
                                    <span class="px-2 py-0.5 rounded bg-zinc-900 text-violet-400 mono text-[10px] font-bold border border-violet-900/30">${res.method}</span>
                                    <span class="text-[10px] text-zinc-500 mono truncate max-w-md" title="${escapeHTML(res.url)}">${escapeHTML(res.url)}</span>
                                </div>
                                <button data-copy-index="${idx}" class="copy-body-btn text-[9px] uppercase tracking-widest text-zinc-600 hover:text-zinc-400 font-bold transition-colors">Copy Body</button>
                            </div>
                            <div class="bg-black/40 rounded-xl p-4 border border-zinc-800/60 overflow-hidden">
                                <pre id="raw-body-${idx}" class="text-[11px] text-zinc-300 mono whitespace-pre-wrap overflow-x-auto leading-relaxed max-h-[400px] select-text"></pre>
                            </div>
                        </div>
                    `).join('')}
                </div>
                <div class="mt-6 flex justify-end shrink-0">
                    <button onclick="document.getElementById('modal-container').classList.remove('active')" class="px-6 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 font-bold rounded-xl transition-all text-xs uppercase tracking-widest">CLOSE</button>
                </div>
            </div>
        `;

        // 2. Inject raw data via textContent to prevent ANY HTML interpretation or interference
        responses.forEach((res, idx) => {
            const pre = document.getElementById(`raw-body-${idx}`);
            if (pre) {
                pre.textContent = RAW_DATA_CACHE.bodies[idx];
            }
        });

        // 3. Attach safe handlers
        document.getElementById('copy-all-raw')?.addEventListener('click', function() {
            copyToClipboard(JSON.stringify(RAW_DATA_CACHE.full, null, 2), this);
        });

        document.querySelectorAll('.copy-body-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                const idx = parseInt(this.dataset.copyIndex);
                if (!isNaN(idx) && RAW_DATA_CACHE.bodies[idx]) {
                    copyToClipboard(RAW_DATA_CACHE.bodies[idx], this);
                }
            });
        });

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
window.editSidecarName = editSidecarName;
window.addSidecarTag = addSidecarTag;
window.deleteSidecar = deleteSidecar;
window.triggerSidecarCollect = triggerSidecarCollect;
window.setFilterDimension = setFilterDimension;
window.setFilter = setFilter;
window.setHistoryDays = setHistoryDays;
window.setHistoryMetric = setHistoryMetric;
window.toggleHistoryProvider = toggleHistoryProvider;
window.refreshToken = refreshToken;
window.deleteToken = deleteToken;

