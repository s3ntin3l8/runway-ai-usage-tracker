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
    compact: false,
    remaining: false,
    data: []
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
