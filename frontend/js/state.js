/**
 * @typedef {Object} AppState
 * @property {boolean} compact - Toggle compact view mode
 * @property {boolean} remaining - Toggle remaining quota display
 * @property {Array} data - Array of quota limit objects from the API
 */

/**
 * Global application state object
 * @type {AppState}
 */
export const STATE = {
    compact: localStorage.getItem('runway_compact') === 'true',
    remaining: localStorage.getItem('runway_remaining') === 'true',
    theme: (() => {
        const stored = localStorage.getItem('runway_theme');
        if (stored === 'light' || stored === 'dark') return stored;
        // Migrate legacy boolean key
        return localStorage.getItem('runway_bright_mode') === 'true' ? 'light' : 'dark';
    })(),
    githubAuth: { authenticated: false, account: null },
    data: [],
    // Dashboard context filter
    activeFilter: (() => {
        const stored = JSON.parse(localStorage.getItem('runway_active_filter') || 'null');
        if (stored && !['account_label', 'sidecar_id', 'window_type'].includes(stored.dimension)) return null;
        return stored;
    })(),
    filterDimension: (() => {
        const stored = localStorage.getItem('runway_filter_dimension');
        return ['account_label', 'sidecar_id', 'window_type'].includes(stored) ? stored : 'account_label';
    })(),
    // Dashboard reordering
    editMode: false,
    layout: (() => {
        try {
            const stored = JSON.parse(localStorage.getItem('runway_layout') || 'null');
            if (stored && Array.isArray(stored.provider_order) && stored.card_orders && typeof stored.card_orders === 'object') {
                return stored;
            }
        } catch {}
        return { provider_order: [], card_orders: {} };
    })(),
};

/**
 * @typedef {Object} HealthStatusConfig
 * @property {string} dot - CSS class for status indicator dot
 * @property {string} card - CSS class for card styling
 * @property {string} badge - Tailwind color class for badge text
 * @property {string} bar - Hex color for progress bar
 * @property {string} label - Display label for status
 */

/**
 * Health status configuration mapping
 * Maps quota status to visual styling classes and colors
 * @type {Object.<string, HealthStatusConfig>}
 */
export const HEALTH_CONFIG = {
    good:     { dot: 'dot-good',     lamp: 'lamp-good', card: 'health-good',     badge: 'text-good-c',  tag: 'tag-good', bar: '#5af080', label: 'GOOD' },
    warning:  { dot: 'dot-warning',  lamp: 'lamp-warn', card: 'health-warning',  badge: 'text-warn-c',  tag: 'tag-warn', bar: '#ffb000', label: 'WARN' },
    critical: { dot: 'dot-critical', lamp: 'lamp-crit', card: 'health-critical', badge: 'text-crit-c',  tag: 'tag-crit', bar: '#ff3b30', label: 'CRIT' },
    unknown:  { dot: 'dot-unknown',  lamp: 'lamp-unk',  card: 'health-unknown',  badge: 'text-unk-c',   tag: 'tag-unk',  bar: '#5a6068', label: '——'  },
    unlimited:{ dot: 'dot-unlimited',lamp: 'lamp-unlm', card: 'health-unlimited',badge: 'text-unlm-c',  tag: 'tag-unlm', bar: '#d580ff', label: 'UNLM' },
};

/**
 * Error type configuration mapping
 * Maps error types to visual indicators
 */
export const ERROR_TYPES = {
    missing_config: { label: 'CONFIG', icon: '⚙️', color: 'text-zinc-400' },
    auth_failed: { label: 'AUTH', icon: '🔒', color: 'text-red-400' },
    rate_limited: { label: '429', icon: '⏱️', color: 'text-orange-400' },
    timeout: { label: 'T/O', icon: '⏳', color: 'text-amber-400' },
    parse_error: { label: 'PARSE', icon: '🧩', color: 'text-purple-400' },
    api_error: { label: 'API', icon: '⚠️', color: 'text-red-500' },
    unknown: { label: 'ERR', icon: '❌', color: 'text-red-600' }
};
