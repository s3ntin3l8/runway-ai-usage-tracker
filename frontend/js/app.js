import { fetchLimits } from './api.js';
import { STATE, HEALTH_CONFIG, REFRESH_CONFIG } from './state.js';
import { buildCard, buildModalContent } from './components.js';

// Auto-refresh timer reference
let refreshTimer = null;

/**
 * Render quota cards to the grid
 * Builds HTML from STATE.data and populates the grid element
 * Gracefully handles individual card rendering errors
 */
function renderGrid() {
    const grid = document.getElementById('grid');
    let html = '';
    let count = 0;

    STATE.data.forEach(item => {
        try {
            const cardHtml = buildCard(item);
            if (cardHtml) {
                html += cardHtml;
                count++;
            }
        } catch (e) {
            console.error("Failed to render card for:", item, e);
        }
    });

    grid.innerHTML = html;
    document.getElementById('footer-count').textContent = count;
}

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
 * Cycle through auto-refresh intervals
 * OFF → 30s → 60s → 5m → OFF
 */
window.cycleRefreshInterval = function () {
    const intervals = REFRESH_CONFIG.intervals;
    const currentIndex = intervals.indexOf(STATE.refreshInterval);
    const nextIndex = (currentIndex + 1) % intervals.length;
    const nextInterval = intervals[nextIndex];

    setRefreshInterval(nextInterval);
}

/**
 * Set auto-refresh interval
 * @param {string} interval - Interval key ('off', '30s', '60s', '5m')
 */
function setRefreshInterval(interval) {
    STATE.refreshInterval = interval;
    localStorage.setItem('runway_refresh_interval', interval);

    // Clear existing timer
    if (refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
    }

    // Update button UI
    const btn = document.getElementById('toggle-refresh');
    if (btn) {
        btn.innerHTML = REFRESH_CONFIG.labels[interval];
        btn.classList.toggle('refresh-active', interval !== 'off');
    }

    // Set new timer if not off
    if (interval !== 'off' && REFRESH_CONFIG.ms[interval]) {
        refreshTimer = setInterval(() => {
            console.log(`Auto-refresh triggered (${interval})`);
            loadData();
        }, REFRESH_CONFIG.ms[interval]);
        console.log(`Auto-refresh enabled: ${interval}`);
    } else {
        console.log('Auto-refresh disabled');
    }
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

    // Initialize refresh interval
    const refreshBtn = document.getElementById('toggle-refresh');
    if (refreshBtn) {
        refreshBtn.innerHTML = REFRESH_CONFIG.labels[STATE.refreshInterval];
        refreshBtn.classList.toggle('refresh-active', STATE.refreshInterval !== 'off');
    }
    if (STATE.refreshInterval !== 'off') {
        setRefreshInterval(STATE.refreshInterval);
    }

    // Initialize theme
    applyTheme();
    const themeBtn = document.getElementById('toggle-theme');
    if (themeBtn) {
        themeBtn.innerHTML = STATE.brightMode ? '🌙' : '☀️';
        themeBtn.title = STATE.brightMode ? 'Switch to dark mode' : 'Switch to bright mode';
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
    const item = STATE.data.find(d => d.service === serviceName);
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
    const grid = document.getElementById('grid');
    const loading = document.getElementById('loading');
    const errorBanner = document.getElementById('error-banner');
    const refreshBtn = document.getElementById('refresh-btn');
    const refreshIcon = document.getElementById('refresh-icon');
    const lastUpdated = document.getElementById('last-updated');

    grid.innerHTML = '';
    grid.classList.add('hidden');
    loading.classList.remove('hidden');
    errorBanner.classList.add('hidden');
    refreshBtn.disabled = true;
    refreshIcon.style.animation = 'spin 1s linear infinite';
    refreshIcon.style.transformOrigin = 'center';

    try {
        const json = await fetchLimits();
        STATE.data = json.limits;
        renderGrid();

        const now = new Date();
        lastUpdated.textContent = `Updated ${now.toLocaleTimeString()}`;
        lastUpdated.classList.remove('hidden');

    } catch (err) {
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
        refreshBtn.disabled = false;
        refreshIcon.style.animation = 'none';
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
    const item = STATE.data.find(d => d.service === serviceName);
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
    const refreshBtn = document.getElementById('refresh-btn');
    const modalBackdrop = document.getElementById('modal-backdrop');

    refreshBtn.addEventListener('click', loadData);

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
