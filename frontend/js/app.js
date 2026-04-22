import { fetchLimits, getGitHubOAuthStatus, initGitHubOAuth, pollGitHubOAuth, logoutGitHub, fetchHistory, fetchSettings, fetchFleet, patchSidecar, deleteSidecarAPI, fetchTokenHealth, postTokenRefresh, forceCollect, fetchProviderConfigs, putProviderConfig, fetchAppConfig, putAppConfig, collectProvider, getDashboardLayout, putDashboardLayout } from './api.js';
import { STATE, HEALTH_CONFIG } from './state.js';
import { applyOrder, cardKey, extractProviderOrder, extractCardOrder } from './layout.js';
import { ensureSortable } from './sortable.js';
import { buildCard, buildModalContent, buildGitHubOAuthModal, buildProviderSection, buildProviderSummaryCard, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildProviderModal, buildProviderSparklineStrip } from './components.js';
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

/**
 * Toggle bright/dark mode
 */
window.toggleTheme = function () {
    STATE.theme = STATE.theme === 'light' ? 'dark' : 'light';
    localStorage.setItem('runway_theme', STATE.theme);

    applyTheme();
    updateThemeIcon();
}

function updateThemeIcon() {
    const sunIcon = document.getElementById('theme-icon-sun');
    const moonIcon = document.getElementById('theme-icon-moon');
    if (!sunIcon || !moonIcon) return;
    sunIcon.classList.toggle('hidden', STATE.theme === 'light');
    moonIcon.classList.toggle('hidden', STATE.theme === 'dark');
}

/**
 * Apply current theme to document
 */
function applyTheme() {
    document.documentElement.dataset.theme = STATE.theme;
}

/**
 * Start the HUD header clock (UTC time + T+ elapsed since last fetch).
 */
function startHudClock() {
    const clockEl = document.getElementById('utc-clock');
    const tickEl  = document.getElementById('last-tick');
    setInterval(() => {
        if (clockEl) {
            const n = new Date();
            clockEl.textContent =
                String(n.getUTCHours()).padStart(2, '0') + ':' +
                String(n.getUTCMinutes()).padStart(2, '0') + ':' +
                String(n.getUTCSeconds()).padStart(2, '0') + 'Z';
        }
        if (tickEl && window._lastFetchTime) {
            const e = Math.floor((Date.now() - window._lastFetchTime) / 1000);
            tickEl.textContent =
                'T+' +
                String(Math.floor(e / 3600)).padStart(2, '0') + ':' +
                String(Math.floor((e % 3600) / 60)).padStart(2, '0') + ':' +
                String(e % 60).padStart(2, '0');
        }
    }, 1000);
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
    startHudClock();

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
    
    // Wake Trigger: nudge the poller and refresh data when the tab becomes visible.
    // Poller wake: 30s debounce. Data refresh: only when tab was hidden for >5 min.
    let lastWakeTime = 0;
    let tabHiddenAt = 0;
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') {
            tabHiddenAt = Date.now();
        } else {
            const now = Date.now();
            if (now - lastWakeTime > 30000) {
                lastWakeTime = now;
                import('./api.js').then(m => m.postWake());
            }
            if (tabHiddenAt && now - tabHiddenAt > 5 * 60 * 1000) {
                loadDashboard();
            }
            tabHiddenAt = 0;
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
    if (icon) icon.classList.add('animate-spin');

    // Enforce a visible minimum so the spinner is never just a flash
    // (collect_all returns cached data in <100ms when providers are within TTL)
    const minVisible = new Promise(r => setTimeout(r, 1500));

    try {
        await forceCollect();
    } catch (e) {
        console.warn('Force collect error (server may be restarting):', e.message);
    }
    await Promise.all([loadData(), minVisible]);

    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove('animate-spin');
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

    initUI();  // also calls initHistoryView() and switchView('dashboard') → loadDashboard if empty

    // Check auth; only trigger an explicit load if switchView hasn't already fetched.
    checkAuth().then(authorized => {
        if (authorized) {
            if (STATE.data.length === 0) loadDashboard();
            // Auto-refresh every 5 minutes — skip silently when the tab is hidden.
            refreshTimer = setInterval(() => {
                if (!document.hidden) loadDashboard();
            }, 5 * 60 * 1000);
        }
    });
});

window.handleResetProvider = async function(event, provider, accountId) {
    const btn = event?.target ?? null;
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
        <div style="padding:3rem;text-align:center;">
            <div style="display:inline-block;width:28px;height:28px;border:2px solid var(--hairline-strong);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite;margin-bottom:1rem;"></div>
            <p class="mono" style="font-size:11px;font-weight:700;letter-spacing:0.14em;color:var(--text-muted);text-transform:uppercase;">Fetching raw data from ${escapeHTML(providerId)}…</p>
            <p class="mono" style="font-size:10px;color:var(--text-dim);margin-top:6px;">May take up to 30s if triggering a fresh collection cycle.</p>
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
            <div style="display:flex;flex-direction:column;gap:0;">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1.25rem;padding-bottom:1rem;border-bottom:1px solid var(--hairline-strong);">
                    <div>
                        <div class="mono" style="font-size:13px;font-weight:700;color:var(--text);text-transform:uppercase;letter-spacing:0.1em;">RAW DATA: ${escapeHTML(providerId)}</div>
                        <div class="mono" style="font-size:10px;color:var(--text-dim);margin-top:3px;">HTTP interception bundle — ${responses.length} response${responses.length !== 1 ? 's' : ''} captured</div>
                    </div>
                    <div style="display:flex;gap:8px;align-items:center;">
                        <button id="copy-all-raw" class="toggle-btn" style="font-size:10px;padding:3px 10px;">Copy All</button>
                        <button class="icon-btn" onclick="document.getElementById('modal-container').classList.remove('active')" style="font-size:1.1rem;width:28px;height:28px;">✕</button>
                    </div>
                </div>
                <div id="raw-responses-list" style="display:flex;flex-direction:column;gap:1.5rem;">
                    ${responses.length === 0 ? `
                        <div style="padding:2rem;text-align:center;border:1px dashed var(--hairline-strong);">
                            <p class="mono" style="color:var(--text-muted);font-size:12px;">No HTTP requests were captured during the collection cycle.</p>
                            <p class="mono" style="color:var(--text-dim);font-size:10px;margin-top:6px;">Data was served from cache or an internal strategy.</p>
                        </div>
                    ` : responses.map((res, idx) => `
                        <div style="border-bottom:1px solid var(--hairline);padding-bottom:1.25rem;">
                            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
                                <div style="display:flex;align-items:center;gap:6px;">
                                    <span class="tag mono" style="font-size:10px;padding:2px 6px;">${res.status}</span>
                                    <span class="mono" style="font-size:10px;font-weight:700;color:var(--accent);">${res.method}</span>
                                    <span class="mono" style="font-size:10px;color:var(--text-muted);word-break:break-all;" title="${escapeHTML(res.url)}">${escapeHTML(res.url)}</span>
                                </div>
                                <button data-copy-index="${idx}" class="copy-body-btn mono" style="font-size:9px;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-dim);background:none;border:none;cursor:pointer;font-weight:700;">Copy Body</button>
                            </div>
                            <div style="background:var(--bg);border:1px solid var(--hairline);padding:12px;overflow:hidden;">
                                <pre id="raw-body-${idx}" class="mono" style="font-size:11px;color:var(--text-muted);white-space:pre-wrap;overflow-x:auto;line-height:1.6;max-height:400px;overflow-y:auto;"></pre>
                            </div>
                        </div>
                    `).join('')}
                </div>
                <div style="margin-top:1.5rem;display:flex;justify-content:flex-end;">
                    <button onclick="document.getElementById('modal-container').classList.remove('active')" class="toggle-btn mono" style="font-size:11px;padding:6px 20px;text-transform:uppercase;letter-spacing:0.1em;">CLOSE</button>
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
            <div style="padding:2.5rem;text-align:center;">
                <div style="font-size:2rem;margin-bottom:1rem;color:var(--crit);">⚠</div>
                <div class="mono" style="font-size:13px;font-weight:700;color:var(--text);text-transform:uppercase;letter-spacing:0.1em;margin-bottom:0.5rem;">Debug Fetch Failed</div>
                <p class="mono" style="font-size:11px;color:var(--text-muted);margin-bottom:1.5rem;">${escapeHTML(err.message)}</p>
                <button onclick="document.getElementById('modal-container').classList.remove('active')" class="toggle-btn mono" style="font-size:11px;padding:6px 20px;text-transform:uppercase;letter-spacing:0.1em;">DISMISS</button>
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

