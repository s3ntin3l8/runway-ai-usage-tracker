import { fetchWithAuth, fetchLimits, getGitHubOAuthStatus, initGitHubOAuth, pollGitHubOAuth, logoutGitHub, fetchSettings, fetchFleet, patchSidecar, deleteSidecarAPI, fetchTokenHealth, postTokenRefresh, forceCollect, fetchProviderConfigs, putProviderConfig, fetchAppConfig, putAppConfig, collectProvider, getDashboardLayout, putDashboardLayout } from './api.js';
import { STATE, HEALTH_CONFIG } from './state.js';
import { applyOrder, cardKey, extractProviderOrder, extractCardOrder } from './layout.js';
import { ensureSortable } from './sortable.js';
import { buildGitHubOAuthModal, buildFleetView, buildTokenHealthPanel, escapeHTMLAttr, buildProviderSparklineStrip } from './components.js';
import { updateCharts, destroyCharts } from './charts.js';
import { escapeHTML } from './utils/html.js';
import { showConfirm } from './utils/modal-dialog.js';
import { loadDashboard, initDashboardView, setFilter, setFilterDimension } from './views/dashboard.js';

// Non-dashboard views are lazy-loaded — they aren't needed for the initial
// paint, and statically importing them shipped ~78 KB of JS that the cold
// load never executed. Each module is fetched on first navigation and cached
// for subsequent visits; init() runs exactly once per module.
const _viewModules = {};
const _viewInitDone = {};
function loadViewModule(id) {
    return _viewModules[id] ??= import(`./views/${id}.js`);
}

// Alias for backwards compatibility
window.loadDashboard = loadDashboard;

// History data is now fully managed via views/history.js

// Auto-refresh timer reference
let refreshTimer = null;
let githubPollTimer = null;
let loadDataGeneration = 0; // Prevents stale fetch responses from overwriting newer data

/**
 * View Management
 *
 * Convention: each lazy view module exports `load<Cap>View()` and optionally
 * `init<Cap>View()` (capitalised id, e.g. id 'history' → loadHistoryView /
 * initHistoryView). The dashboard view is statically imported above because
 * it's the cold-load view and has a STATE.data guard that doesn't fit the
 * convention; everything else is dispatched generically.
 */
const KNOWN_VIEWS = ['dashboard', 'history', 'fleet', 'settings', 'auth', 'error'];
const LAZY_VIEWS = ['history', 'settings', 'fleet'];

const _cap = id => id[0].toUpperCase() + id.slice(1);

window.switchView = async function(viewId) {
    if (!KNOWN_VIEWS.includes(viewId)) viewId = 'dashboard';
    if (STATE.editMode) exitEditMode();
    // Hide all views
    document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
    document.getElementById(`view-${viewId}`).classList.remove('hidden');

    // Update nav links
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    document.getElementById(`nav-${viewId}`).classList.add('active');

    // Mobile chrome: expose the active view on <body> (CSS keys per-view
    // header buttons off it at ≤640px; inert on desktop) and sync the
    // bottom tab bar's active state.
    document.body.dataset.view = viewId;
    syncMobileTabs();

    // Sync URL (no scroll jump)
    const target = `#${viewId}`;
    if (location.hash !== target) {
        history.replaceState(null, '', target);
    }

    if (viewId === 'dashboard') {
        if (STATE.data.length === 0) await loadDashboard();
        return;
    }
    if (LAZY_VIEWS.includes(viewId)) {
        const m = await loadViewModule(viewId);
        const initFn = m[`init${_cap(viewId)}View`];
        if (initFn && !_viewInitDone[viewId]) {
            _viewInitDone[viewId] = true;
            initFn();
        }
        m[`load${_cap(viewId)}View`]?.();
    }
};

// Re-exports for onclick handlers in index.html are now handled in views/history.js initHistoryView()

/**
 * Mobile bottom tab bar (≤640px).
 *
 * The bar has five tabs but the app has four views: "Dashboard" and "Quotas"
 * both map to the dashboard view, split by `body[data-mobile-subview]`
 * ("overview" = hero only, "quotas" = filter bar + provider cards). The
 * attribute is inert on desktop — only the ≤640px CSS layer reads it.
 */
function syncMobileTabs() {
    const bar = document.getElementById('mobile-tabbar');
    if (!bar) return;
    const view = document.body.dataset.view || 'dashboard';
    const sub = document.body.dataset.mobileSubview || 'overview';
    const active = view === 'dashboard' ? sub : view;
    bar.querySelectorAll('.mtab').forEach(t =>
        t.classList.toggle('active', t.dataset.tab === active));
    // The header search drawer is Quotas-scoped — collapse it elsewhere.
    if (active !== 'quotas') closeHeaderSearch();
}

/** Header search drawer (mobile Quotas) — mirrors into #card-search so the
 *  dashboard's existing search filtering runs unchanged. */
function openHeaderSearch() {
    const drawer = document.getElementById('header-search');
    const btn = document.getElementById('search-btn');
    if (!drawer) return;
    drawer.classList.add('open');
    btn?.setAttribute('aria-pressed', 'true');
    setTimeout(() => document.getElementById('header-search-input')?.focus(), 80);
}

function closeHeaderSearch() {
    const drawer = document.getElementById('header-search');
    const btn = document.getElementById('search-btn');
    if (!drawer || !drawer.classList.contains('open')) return;
    drawer.classList.remove('open');
    btn?.setAttribute('aria-pressed', 'false');
    document.getElementById('header-search-input')?.blur();
}

function initHeaderSearch() {
    const btn = document.getElementById('search-btn');
    const input = document.getElementById('header-search-input');
    btn?.addEventListener('click', () => {
        const drawer = document.getElementById('header-search');
        if (drawer?.classList.contains('open')) closeHeaderSearch();
        else openHeaderSearch();
    });
    document.getElementById('hs-close')?.addEventListener('click', closeHeaderSearch);
    input?.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeHeaderSearch(); });
    input?.addEventListener('input', () => {
        const cardSearch = document.getElementById('card-search');
        if (!cardSearch) return;
        cardSearch.value = input.value;
        cardSearch.dispatchEvent(new Event('input', { bubbles: true }));
    });
}

function initMobileTabbar() {
    const bar = document.getElementById('mobile-tabbar');
    if (!bar) return;
    if (!document.body.dataset.mobileSubview) {
        document.body.dataset.mobileSubview = 'overview';
    }
    bar.addEventListener('click', (e) => {
        const tab = e.target.closest('.mtab');
        if (!tab) return;
        const t = tab.dataset.tab;
        if (t === 'overview' || t === 'quotas') {
            document.body.dataset.mobileSubview = t;
            switchView('dashboard');
        } else {
            switchView(t);
        }
    });
}

/**
 * Authentication Management
 */
async function checkAuth() {
    const nav = document.getElementById('main-nav');
    try {
        const settings = await fetchSettings();

        // Reflect the real backend version in the brand chip (Release-Please
        // bumps pyproject.toml; the static markup is just a pre-load fallback).
        if (settings.version) {
            const chip = document.getElementById('brand-chip');
            if (chip) chip.textContent = `v${settings.version}`;
        }

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
 * Toggle bright/dark mode
 */
window.toggleTheme = function () {
    STATE.theme = STATE.theme === 'light' ? 'dark' : 'light';
    localStorage.setItem('runway_theme', STATE.theme);

    applyTheme();
    updateThemeIcon();
};

// Set theme to a specific value — used by the Settings › Display control
// (the header toggle is hidden on phones). Mirrors window.setAccent.
window.setTheme = function (value) {
    if (value !== 'light' && value !== 'dark') return;
    STATE.theme = value;
    localStorage.setItem('runway_theme', value);
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
 * Apply current accent color to document
 */
function applyAccent() {
    const el = document.documentElement;
    if (STATE.accent && STATE.accent !== 'orange') {
        el.dataset.accent = STATE.accent;
    } else {
        delete el.dataset.accent;
    }
}

window.setAccent = function (color) {
    STATE.accent = color;
    localStorage.setItem('runway_accent', color);
    applyAccent();
};

/**
 * Apply current display preferences (column count, card chrome) to document.
 * Reflected as data attributes on <html>; CSS variants key off these.
 */
function applyDisplayPrefs() {
    document.documentElement.dataset.cols = STATE.display.cols;
    document.documentElement.dataset.chrome = STATE.display.chrome;
    document.documentElement.dataset.compact = STATE.display.compact;
}

/**
 * Update a display preference (cols | chrome | compact), persist to
 * localStorage, and re-apply the data attributes so the dashboard reflows
 * live.
 */
window.setDisplayPref = function (key, value) {
    if (key !== 'cols' && key !== 'chrome' && key !== 'compact') return;
    STATE.display[key] = value;
    localStorage.setItem('runway_display_' + key, value);
    applyDisplayPrefs();
};

/**
 * Start the HUD header clock (UTC time + T+ elapsed · since HH:MMZ).
 */
function startHudClock() {
    const tickEl = document.getElementById('last-tick');
    setInterval(() => {
        if (!tickEl) return;
        if (!window._lastFetchTime) {
            tickEl.textContent = 'syncing…';
            return;
        }
        const e = Math.floor((Date.now() - window._lastFetchTime) / 1000);
        let label;
        if (e < 60)        label = `${e}s ago`;
        else if (e < 3600) label = `${Math.floor(e / 60)}m ago`;
        else               label = `${Math.floor(e / 3600)}h ago`;
        tickEl.textContent = `synced ${label}`;
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

    // Provider sections grid in the new dashboard
    const sectionsContainer = document.getElementById('dashboard-sections');
    if (sectionsContainer) {
        // Fleet commander mode: cards are article.fc inside .fleet-stack
        const fleetStack = sectionsContainer.querySelector('.fleet-stack');
        if (fleetStack) {
            _providerSortable = new Sortable(fleetStack, {
                animation: 150,
                draggable: 'article.fc',
                onEnd: onProviderDrop,
            });
        } else {
            // Legacy flat-card mode: sections carry [data-provider-id]
            _providerSortable = new Sortable(sectionsContainer, {
                animation: 150,
                draggable: '.section[data-provider-id]',
                onEnd: onProviderDrop,
            });
            // Per-card grids within each provider section
            document.querySelectorAll('#dashboard-sections .hz-grid').forEach(container => {
                const section = container.closest('.section[data-provider-id]');
                if (!section) return;
                const s = new Sortable(container, {
                    animation: 150,
                    draggable: '[data-card-key]',
                    onEnd: () => onCardDrop(section.dataset.providerId, container),
                });
                _cardSortables.push(s);
            });
        }
    }

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
    const sectionsContainer = document.getElementById('dashboard-sections');
    const order = extractProviderOrder(sectionsContainer);
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
    // Re-attach sortables for any card grids in the open modal
    document.querySelectorAll('#modal-content .hz-grid').forEach(container => {
        const section = container.closest('[data-provider-id]');
        if (!section) return;
        const s = new Sortable(container, {
            animation: 150,
            draggable: '[data-card-key]',
            onEnd: () => onCardDrop(section.dataset.providerId, container),
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

    // Initialize theme and accent
    applyTheme();
    applyAccent();
    updateThemeIcon();
    applyDisplayPrefs();
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

    // Mobile bottom tab bar + header search drawer (no-ops on desktop —
    // both elements are display:none outside the ≤640px layer)
    initMobileTabbar();
    initHeaderSearch();

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

    // Initialize dashboard view event listeners. History/Forecast init runs
    // lazily on first switchView() to that tab — keeps their modules off the
    // cold-load critical path.
    initDashboardView();

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
        // Settings module is already loaded if this pane is active.
        loadViewModule('settings').then(m => m.renderProvidersSection(pane));
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
    const ok = await showConfirm(
        'Disconnect GitHub',
        'Disconnect GitHub account?',
        { okLabel: 'Disconnect', danger: true }
    );
    if (ok) {
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

function closeModal() {
    const container = document.getElementById('modal-container');
    container.classList.remove('active');
    document.body.style.overflow = '';
}

function showFlashMessage(text, ms = 3000) {
    const id = 'runway-flash-msg';
    document.getElementById(id)?.remove();
    const el = document.createElement('div');
    el.id = id;
    el.textContent = text;
    Object.assign(el.style, {
        position: 'fixed', top: '64px', right: '20px', zIndex: '9999',
        padding: '8px 14px', fontSize: '11px', fontWeight: '600',
        letterSpacing: '0.06em', textTransform: 'uppercase',
        background: 'var(--surface-2)', color: 'var(--ink)',
        border: '1px solid var(--accent)', borderRadius: '4px',
        boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
        opacity: '0', transition: 'opacity 200ms ease',
        pointerEvents: 'none', maxWidth: '360px',
    });
    document.body.appendChild(el);
    requestAnimationFrame(() => { el.style.opacity = '1'; });
    setTimeout(() => {
        el.style.opacity = '0';
        setTimeout(() => el.remove(), 250);
    }, ms);
}

window.forceRefresh = async function() {
    const btn = document.getElementById('refresh-btn');
    const icon = document.getElementById('refresh-icon');
    if (btn) btn.disabled = true;
    if (icon) icon.classList.add('animate-spin');

    // Enforce a visible minimum so the spinner is never just a flash
    // (collect_all returns cached data in <100ms when providers are within TTL)
    const minVisible = new Promise(r => setTimeout(r, 1500));

    let triggered = 0;
    try {
        const res = await forceCollect();
        triggered = res?.sidecars_triggered ?? 0;
    } catch (e) {
        console.warn('Force collect error (server may be restarting):', e.message);
    }
    await Promise.all([loadData(), minVisible]);

    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove('animate-spin');

    if (triggered > 0) {
        showFlashMessage(
            `Refreshed · ${triggered} sidecar${triggered === 1 ? '' : 's'} notified for next check-in`
        );
    }
};

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (refreshTimer) {
        clearInterval(refreshTimer);
    }
});

// Initial load
document.addEventListener('DOMContentLoaded', async () => {
    const modalBackdrop = document.getElementById('modal-backdrop');

    // Modal close via backdrop click or Escape key
    modalBackdrop?.addEventListener('click', closeModal);
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeModal();
    });

    // Timezone is already on window.runwayConfig — it was injected synchronously
    // by an inline <script> in <head> (see app/main.py:147-154) before any
    // module evaluated. fetchAppConfig() here is a refresh of that snapshot;
    // it runs off the critical path so it doesn't gate the dashboard render.
    // INVARIANT: if the inline injection is ever removed from main.py, this
    // refresh must move back to an awaited step before initUI() to avoid
    // first-paint TZ regression.
    const configRefreshP = fetchAppConfig()
        .then(cfg => import('./utils/tz.js').then(m => m.setRunwayConfig(cfg)))
        .catch(() => {});

    const authenticated = await checkAuth().catch(() => false);
    if (!authenticated) {
        void configRefreshP;
        return;
    }

    await initUI();

    refreshTimer = setInterval(() => {
        if (!document.hidden) loadDashboard();
    }, 5 * 60 * 1000);
    void configRefreshP;
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
        // 1. Reset backoff/failure state
        const resetResp = await fetch(`/api/v1/usage/reset/${provider}${query}`, { method: 'POST' });
        if (!resetResp.ok) throw new Error('Reset failed');

        if (btn) btn.innerText = 'COLLECTING...';

        // 2. Force immediate re-collection
        const collectResp = await fetch(`/api/v1/usage/collect/${provider}${query}`, { method: 'POST' });
        if (!collectResp.ok) throw new Error('Collection failed');
        const collectData = await collectResp.json();

        // 3. Only show SUCCESS if we actually got cards back (not an error card)
        // Note: collect_one returns the raw cards. If the first one is an error, it failed.
        if (collectData.cards > 0) {
            if (btn) btn.innerText = 'SUCCESS!';
            setTimeout(() => {
                const modalContainer = document.getElementById('modal-container');
                if (modalContainer) modalContainer.classList.remove('active');
                loadDashboard();
            }, 1000);
        } else {
            throw new Error('Still rate limited');
        }
    } catch (err) {
        if (btn) {
            btn.innerText = originalText;
            btn.disabled = false;
        }
        // If still limited, the dashboard will refresh and show the updated backoff anyway
        loadDashboard();
    }
};

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
        const resp = await fetchWithAuth(`/api/v1/system/debug/raw/${providerId}`);
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

// Expose functions needed by inline onclick handlers in HTML. Lazy-view
// handlers can only fire after that view has rendered its DOM (so the module
// is already cached by then), but the shim still loads it on demand to defend
// against any stray pre-render trigger.
window.switchView = switchView;
window.setFilterDimension = setFilterDimension;
window.setFilter = setFilter;

function bindLazy(viewId, ...names) {
    for (const name of names) {
        window[name] = (...args) => loadViewModule(viewId).then(m => m[name](...args));
    }
}
bindLazy('fleet',    'editSidecarName', 'addSidecarTag', 'deleteSidecar', 'toggleSidecarEnabled');
bindLazy('history',  'setHistoryDays', 'setHistoryMetric', 'toggleHistoryProvider');
bindLazy('settings', 'refreshToken', 'deleteToken');
