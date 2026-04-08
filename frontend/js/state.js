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
    showHidden: localStorage.getItem('runway_show_hidden') === 'true',
    disabledServices: JSON.parse(localStorage.getItem('runway_disabled_services') || '[]'),
    refreshInterval: localStorage.getItem('runway_refresh_interval') || 'off',
    brightMode: localStorage.getItem('runway_bright_mode') === 'true',
    data: []
};

/**
 * Auto-refresh interval configuration
 */
export const REFRESH_CONFIG = {
    intervals: ['off', '30s', '60s', '5m'],
    ms: {
        'off': null,
        '30s': 30000,
        '60s': 60000,
        '5m': 300000
    },
    labels: {
        'off': '🔄 OFF',
        '30s': '🔄 ● 30s',
        '60s': '🔄 ● 60s',
        '5m': '🔄 ● 5m'
    }
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
    good:     { dot: 'dot-good',     card: 'health-good',     badge: 'text-emerald-400', bar: '#22c55e', label: 'GOOD' },
    warning:  { dot: 'dot-warning',  card: 'health-warning',  badge: 'text-amber-400',   bar: '#f59e0b', label: 'WARN' },
    critical: { dot: 'dot-critical', card: 'health-critical', badge: 'text-red-400',      bar: '#ef4444', label: 'CRIT' },
    unknown:  { dot: 'dot-unknown',  card: 'health-unknown',  badge: 'text-zinc-500',     bar: '#3f3f46', label: '——' },
    unlimited:{ dot: 'dot-unlimited',card: 'health-unlimited',badge: 'text-violet-400',   bar: '#8b5cf6', label: '∞' },
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
