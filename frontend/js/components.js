import { HEALTH_CONFIG, STATE, ERROR_TYPES } from './state.js';
import { pickBucketSeconds } from './charts.js';
import { cardKey, applyOrder } from './layout.js';
import { formatLocalDateTime } from './utils/tz.js';
import { escapeHTML, escapeHTMLAttr, safeUrl } from './utils/html.js';
import { formatNumber, formatCurrency } from './utils/format.js';

// Re-exported so existing `import { escapeHTMLAttr } from './components.js'`
// callers keep working without touching their imports.
export { escapeHTMLAttr };

function _forecastSeriesKey(entry) {
    return [
        entry.provider_id || '',
        entry.account_id || '',
        entry.service_name || '',
        entry.variant || '',
        entry.model_id || '',
        entry.window_type || '',
        entry.unit_type || '',
    ].join('||');
}

// Display maps for composing card subtitles from canonical fields.
const MODEL_DISPLAY_NAMES = {
    'sonnet': 'Sonnet', 'opus': 'Opus', 'haiku': 'Haiku',
    'design': 'Design', 'flash': 'Flash', 'pro': 'Pro',
    'flash-lite': 'Flash Lite',
    'pro-2.5': 'Pro 2.5', 'pro-3.1-preview': 'Pro 3.1 Preview',
    'flash-2.5': 'Flash 2.5', 'flash-lite-2.5': 'Flash Lite 2.5',
};
const WINDOW_DISPLAY_NAMES = {
    'session': 'Session', 'daily': 'Daily',
    'weekly': 'Weekly', 'monthly': 'Monthly',
    // 'rolling' and 'unknown' deliberately omitted — no useful subtitle component.
};

/** Compose a card subtitle from variant + model_id + window_type. */
function cardSubtitleParts(card) {
    if (!card) return [];
    const parts = [];
    if (card.variant) parts.push(String(card.variant));
    if (card.model_id) {
        parts.push(MODEL_DISPLAY_NAMES[card.model_id] || String(card.model_id).replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()));
    }
    const w = WINDOW_DISPLAY_NAMES[card.window_type];
    if (w) parts.push(w);
    return parts;
}

function cardSubtitleText(card) {
    return cardSubtitleParts(card).join(' · ');
}

// _formatTokenShort, formatHumanDelta, providerDisplayLabel moved to ./components/_shared.js
import { _formatTokenShort, formatHumanDelta, providerDisplayLabel } from './components/_shared.js';
export { providerDisplayLabel };


const PROVIDER_ICONS = {
    anthropic: '🟠', gemini: '✨', github: '🐙', chatgpt: '🤖',
    openrouter: '🚀', opencode: '⚡', ollama: '🦙', minimax: '💎',
    kimi_api: '🌊', kimi_coding: '💻', kimi_k2: '🌙', zai: '🌐',
    antigravity: '🪐',
};


/**
 * Returns a styled tier badge based on the tier name
 * @param {string} tier - Tier name (Free, Pro, Team, etc.)
 * @returns {string} HTML for the badge, or empty string if no tier
 */
function getTierBadge(tier) {
    if (!tier && tier !== 0) return '';
    const t = String(tier).toLowerCase();
    let color, bg, border;
    if (t.includes('max')) {
        color = '#c084fc'; bg = 'rgba(192,132,252,0.1)'; border = 'rgba(192,132,252,0.28)';
    } else if (t.includes('pro') || t.includes('premium') || t.includes('plus')) {
        color = 'var(--accent)'; bg = 'var(--accent-dim)'; border = 'var(--accent)';
    } else if (t.includes('team') || t.includes('enterprise') || t.includes('organization')) {
        color = 'var(--unlm)'; bg = 'color-mix(in srgb,var(--unlm) 10%,transparent)'; border = 'color-mix(in srgb,var(--unlm) 35%,transparent)';
    } else {
        color = 'var(--text-dim)'; bg = 'var(--surface-2)'; border = 'var(--hairline-strong)';
    }
    return `<span style="font-size:8px;font-weight:700;padding:1px 5px;border:1px solid ${border};color:${color};background:${bg};text-transform:uppercase;letter-spacing:0.1em;white-space:nowrap;line-height:1.6;">${escapeHTML(String(tier))}</span>`;
}

/**
 * Returns an icon representing the pace/consumption rate
 * @param {string} pace - Pace descriptor (e.g., "Sustainable", "Fast Burn")
 * @returns {string} Emoji icon for the pace
 */
function getPaceIcon(pace) {
    if (!pace) return '';
    const p = pace.toLowerCase();
    const icons = {
        'stable': '🐢',
        'sustainable': '🌱',
        'pending reset': '⏳',
        'exhausted': '🪫',
        'fast burn': '🔥',
        'moderate burn': '⚡',
        'high': '🚀',
        'fatigue': '😫',
        'critical': '🚨',
        'stopped': '⏹️'
    };
    return icons[p] || '';
}

/**
 * @typedef {Object} LimitCard
 * @property {string} service_name - Service name (e.g., "Claude Pro")
 * @property {string} icon - Emoji icon representing the service
 * @property {string} remaining - Remaining capacity (number, percentage, or "ERR")
 * @property {string} unit - Unit of measurement (e.g., "tokens / 5h", "capacity", "%")
 * @property {string} reset - Human-readable time until reset (e.g., "in 2h 30m")
 * @property {string} health - Health status ("good", "warning", "critical", "unknown", "unlimited")
 * @property {string} pace - Burn rate descriptor (e.g., "Sustainable", "Fast Burn")
 * @property {string} detail - Additional details (e.g., usage percentage, last update time)
 * @property {number|null} used_value - Raw used amount
 * @property {number|null} limit_value - Raw limit amount
 * @property {boolean} is_unlimited - Whether the plan has no hard limit
 * @property {string} unit_type - Type of unit ("currency", "tokens", "requests", "minutes", "percent", "generic")
 * @property {string|null} currency - Currency code ("USD", "EUR", "CNY", etc.)
 * @property {string|null} reset_at - ISO 8601 timestamp for reset (for tooltip)
 * @property {string} data_source - Data source indicator ("oauth", "web_api", "local", "cache", "fallback", "api", "sidecar")
 * @property {string|null} usage_url - URL to provider's usage/settings page
 * @property {string|null} updated_at - ISO 8601 timestamp when data was last collected
 */

// formatNumber / formatCurrency live in ./utils/format.js (imported above).

/**
 * Format relative time from ISO timestamp
 * @param {string} isoTimestamp - ISO 8601 timestamp
 * @returns {string} Relative time string (e.g., "2m ago", "1h ago")
 */
function formatRelativeTime(isoTimestamp) {
    if (!isoTimestamp) return '—';
    try {
        const date = new Date(isoTimestamp);
        const now = new Date();
        const diffMs = now - date;
        const diffSecs = Math.floor(diffMs / 1000);
        const diffMins = Math.floor(diffSecs / 60);
        const diffHours = Math.floor(diffMins / 60);
        const diffDays = Math.floor(diffHours / 24);

        if (diffSecs < 60) return 'just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffHours < 24) return `${diffHours}h ago`;
        if (diffDays < 30) return `${diffDays}d ago`;
        return date.toLocaleDateString();
    } catch (e) {
        return '—';
    }
}

/**
 * Format used/limit values based on unit type
 * @param {number} used - Used amount
 * @param {number} limit - Limit amount
 * @param {string} unitType - Type of unit
 * @param {string} currency - Currency code
 * @returns {Object} Formatted {used, limit, unit}
 */
function formatUsageValues(used, limit, unitType, currency) {
    if (unitType === 'currency') {
        return {
            used: formatCurrency(used, currency),
            limit: formatCurrency(limit, currency),
            unit: ''
        };
    } else if (unitType === 'tokens') {
        return {
            used: formatNumber(used),
            limit: formatNumber(limit),
            unit: 'tokens'
        };
    } else if (unitType === 'requests') {
        return {
            used: formatNumber(used),
            limit: formatNumber(limit),
            unit: 'requests'
        };
    } else if (unitType === 'minutes') {
        return {
            used: formatNumber(used),
            limit: formatNumber(limit),
            unit: 'min'
        };
    } else if (unitType === 'percent') {
        return {
            used: used.toFixed(1),
            limit: limit.toFixed(1),
            unit: '%'
        };
    } else {
        return {
            used: formatNumber(used),
            limit: formatNumber(limit),
            unit: ''
        };
    }
}

/**
 * Calculate human-readable relative time delta
 * @param {Date} targetDate - Target date
 * @returns {string} Relative time string (e.g., "2h 30m", "5d 12h")
 */

/**
 * Format reset time for card display (consistent with tooltip)
 * @param {string|null} resetAt - ISO 8601 timestamp or human-readable string
 * @returns {string} Formatted reset string for display
 */
function formatResetDisplay(resetAt) {
    if (!resetAt || resetAt === '—') return '—';

    // If it's not an ISO timestamp (e.g., "Rolling", "Manual"), return as-is
    if (!resetAt.includes('T') && !resetAt.match(/^\d{4}-\d{2}-\d{2}/)) {
        return resetAt;
    }

    try {
        const date = new Date(resetAt);
        if (isNaN(date.getTime())) return resetAt;

        const now = new Date();
        const diffHours = (date - now) / (1000 * 60 * 60);

        // Get day labels (today/tomorrow) and time
        const isSameDay = date.toDateString() === now.toDateString();
        const isTomorrow = !isSameDay && diffHours > 0 && diffHours < 48;

        // Get time in 24h format (local time, no TZ suffix)
        const timeStr = date.toLocaleTimeString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
        });

        // If < 24h away, show local time
        if (diffHours >= 0 && diffHours < 24) {
            if (isSameDay) {
                return `Today ${timeStr}`;
            }
            if (isTomorrow) {
                return `Tomorrow ${timeStr}`;
            }
        }

        // If > 24h away, show "tomorrow" or date + time
        if (diffHours >= 24) {
            if (isTomorrow) {
                return `Tomorrow ${timeStr}`;
            }
            // Show date + time for future resets
            const dateStr = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
            return `${dateStr} ${timeStr}`;
        }

        // Past resets
        if (diffHours < 0) {
            return formatHumanDelta(date);
        }

        // Fallback
        return `Resets at ${timeStr}`;
    } catch (e) {
        return resetAt;
    }
}

/**
 * Format reset time tooltip with absolute time
 * @param {string|null} resetAt - ISO 8601 timestamp
 * @returns {string|null} Formatted tooltip text or null
 */
function formatResetTooltip(resetAt) {
    if (!resetAt || resetAt === '—') return null;

    // If it's not an ISO timestamp, no tooltip needed (e.g., "Rolling", "Manual")
    if (!resetAt.includes('T') && !resetAt.match(/^\d{4}-\d{2}-\d{2}/)) {
        return null;
    }

    try {
        const date = new Date(resetAt);
        const now = new Date();

        // Check if date is valid
        if (isNaN(date.getTime())) return null;

        const diffHours = (date - now) / (1000 * 60 * 60);

        // Don't show tooltip if <24h away - card already shows "Resets at 10:43"
        if (diffHours < 24) {
            return null;
        }

        // Format time locale-aware (12h or 24h based on browser locale)
        const timeStr = date.toLocaleTimeString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            hour12: undefined // Browser decides based on locale
        });

        // Format date locale-aware (e.g., "10 Jan" or "Jan 10")
        const dateStr = date.toLocaleDateString(undefined, {
            day: 'numeric',
            month: 'short'
        });

        // If > 24h away, include date
        return `Resets at ${timeStr} on ${dateStr}`;
    } catch (e) {
        return null;
    }
}

/**
 * Source color mappings (subtle, glassmorphism-friendly)
 */
const SOURCE_COLORS = {
    oauth: 'text-blue-400/80',
    web_api: 'text-violet-400/80',
    local: 'text-emerald-400/80',
    cache: 'text-orange-400/80',
    fallback: 'text-amber-400/80',
    api: 'text-cyan-400/80',
    sidecar: 'text-pink-400/80'
};

const SOURCE_LABELS = {
    oauth: 'OAuth',
    web_api: 'Web API',
    local: 'Local',
    cache: 'Cache',
    fallback: 'Fallback',
    api: 'API',
    sidecar: 'Sidecar'
};

/**
 * Format data source for display in subtitle
 * @param {string} source - Data source key
 * @returns {string} Formatted source HTML or empty string
 */
function formatDataSource(source) {
    if (!source || source === 'unknown') return '';
    const label = SOURCE_LABELS[source] || source;
    const colorClass = SOURCE_COLORS[source] || 'text-zinc-400/80';
    return ` · <span class="${colorClass}">${label}</span>`;
}

/**
 * Calculate percentage used from raw values
 * @param {LimitCard} item - The limit card data
 * @returns {number|null} Percentage used (0-100) or null
 */
function calculateUsedPct(item) {
    // If it's an unlimited plan, return null
    if (item.is_unlimited) return null;

    // Use provided values if available
    if (item.used_value !== null && item.limit_value !== null && item.limit_value > 0) {
        return (item.used_value / item.limit_value) * 100;
    }

    // Fallback: try to parse from detail string
    if (item.detail) {
        const m = item.detail.match(/(\d+(\.\d+)?)%/);
        if (m) return parseFloat(m[1]);
    }

    // Try to parse from remaining field
    if (item.remaining) {
        const m = item.remaining.match(/(\d+(\.\d+)?)%/);
        if (m) {
            const remainingPct = parseFloat(m[1]);
            return 100 - remainingPct;
        }
    }

    return null;
}

const UNLIMITED_GRADIENT = 'linear-gradient(90deg, #ff0080, #ff8c00, #40e0d0)';

/**
 * Build the progress bar HTML shared by compact and standard card layouts.
 * @param {boolean} isUnlimited
 * @param {number} barWidth - 0-100, ignored when isUnlimited
 * @param {string} barColor - CSS color/gradient for the fill (used when not unlimited)
 * @param {string} trackClasses - CSS classes for the track wrapper div
 * @returns {string} HTML string
 */
function buildProgressBar(isUnlimited, barWidth, barColor, trackClasses) {
    const width = isUnlimited ? 100 : barWidth;
    const bg = isUnlimited ? UNLIMITED_GRADIENT : barColor;
    const unlimitedClass = isUnlimited ? ' progress-unlimited' : '';
    return `<div class="${trackClasses}${unlimitedClass}"><div class="progress-fill h-full" style="width:${width}%;background:${bg};"></div></div>`;
}

/**
 * Build the main display value (∞, percentage, or raw remaining) for a card.
 * @param {boolean} isUnlimited
 * @param {boolean} hasPercentage
 * @param {number} displayPct
 * @param {string} remaining - raw remaining string (item.remaining)
 * @param {boolean} isPlaceholder
 * @param {string} sizeClass - e.g. 'text-2xl leading-none' or 'text-4xl'
 * @param {string} [trailing=''] - optional HTML appended after the value span (e.g. unit label)
 * @returns {string} HTML string
 */
function buildMainDisplay(isUnlimited, hasPercentage, displayPct, remaining, isPlaceholder, sizeClass, trailing = '') {
    const valueClass = isPlaceholder ? 'text-zinc-600' : 'text-zinc-50';
    if (isUnlimited)
        return `<span class="${sizeClass} font-black tracking-tighter text-violet-400">∞</span>`;
    if (hasPercentage)
        return `<span class="${sizeClass} font-black tracking-tighter ${valueClass}">${displayPct.toFixed(1)}%</span>`;
    return `<span class="${sizeClass} font-black tracking-tighter ${valueClass}">${escapeHTML(remaining)}</span>${trailing}`;
}


/**
 * Build HTML for the detail modal content
 * @param {LimitCard} item - The limit card data
 * @returns {string} HTML string for modal content
 */
export function buildModalContent(item) {
    const isUnlimited = item.is_unlimited || item.health === 'unlimited';
    let h = HEALTH_CONFIG[item.health] || HEALTH_CONFIG.unknown;

    // Categorize error if present
    if (item.health === 'critical' && item.error_type && ERROR_TYPES[item.error_type]) {
        const errConfig = ERROR_TYPES[item.error_type];
        h = { ...h, badge: errConfig.color, label: errConfig.label };
    }

    const healthBoxClasses = {
        good:      'bg-emerald-500/10 border-emerald-500/20',
        warning:   'bg-amber-500/10 border-amber-500/20',
        critical:  'bg-red-500/10 border-red-500/20',
        unknown:   'bg-zinc-800/50 border-zinc-700/50',
        unlimited: 'bg-violet-500/10 border-violet-500/20',
    };

    const usedPct = calculateUsedPct(item);

    const formatted = formatUsageValues(
        item.used_value,
        item.limit_value,
        item.unit_type || 'generic',
        item.currency
    );

    const sourceLabel = SOURCE_LABELS[item.data_source] || item.data_source;
    const sourceColor = SOURCE_COLORS[item.data_source] || 'text-zinc-400';
    const resetTime = item.reset_at ? formatLocalDateTime(item.reset_at, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : 'Never';
    const updatedTime = formatRelativeTime(item.updated_at);

    const isAuthFailed = item.error_type === 'auth_failed';
    const retryButton = isAuthFailed ? `
        <button
            onclick="event.stopPropagation(); window.handleResetProvider(event, '${escapeHTMLAttr(item.provider_id)}', '${escapeHTMLAttr(item.account_id)}')"
            style="margin-top:16px;width:100%;padding:10px;background:var(--accent);color:var(--bg);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;"
            class="transition-all">Retry Authentication</button>
    ` : '';

    const safeUsageUrl = safeUrl(item.usage_url);
    const linkButton = safeUsageUrl ? `
        <a href="${safeUsageUrl}" target="_blank" rel="noopener noreferrer" class="icon-btn w-10 h-10 flex items-center justify-center transition-colors" style="color:var(--text-muted);" title="Open usage page">
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
        </a>
    ` : '';

    return `
        <div class="modal-header" style="border-bottom:1px solid var(--hairline);padding-bottom:16px;margin-bottom:16px;">
            <div class="flex items-center justify-between mb-4">
                <div class="flex items-center gap-3">
                    <span style="font-size:1.8rem;">${escapeHTML(item.icon)}</span>
                    <div>
                        <h2 style="font-size:1.1rem;font-weight:700;color:var(--text);letter-spacing:0.04em;">${escapeHTML(item.service_name)}</h2>
                        ${_windowSubtitle(item)}
                        <div class="flex items-center gap-2 mt-0.5">
                            <span class="tag ${h.tag}">${h.label}</span>
                            ${getTierBadge(item.tier)}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-1">
                    ${linkButton}
                    <button id="refresh-provider-btn" title="Refresh now" class="icon-btn w-10 h-10 flex items-center justify-center transition-colors" style="color:var(--text-muted);">
                        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
                    </button>
                    <button id="close-modal" class="icon-btn w-10 h-10 flex items-center justify-center transition-colors" style="color:var(--text-muted);">
                        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                    </button>
                </div>
            </div>

            <div class="mt-4 flex flex-col gap-2">
                <div class="flex items-baseline justify-between">
                    <span class="readout ${h.tag.replace('tag-', 'readout-')}">
                        ${isUnlimited ? '∞' : (usedPct ? usedPct.toFixed(1) + '%' : escapeHTML(item.remaining))}
                    </span>
                    <span style="font-size:10px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">${isUnlimited ? 'Unlimited Capacity' : 'Used Capacity'}</span>
                </div>

                <div class="progress-track h-1.5 w-full mt-2 overflow-hidden" style="background:var(--surface-2);">
                    <div class="progress-fill h-full"
                         style="width: ${isUnlimited ? 100 : (usedPct || 0)}%; background: ${isUnlimited ? 'linear-gradient(90deg, var(--crit), var(--warn), var(--accent-cool))' : h.bar};">
                    </div>
                </div>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mt-2">
            <div class="modal-detail-item flex flex-col gap-1">
                <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">Usage Value</span>
                <span class="mono" style="font-size:1rem;font-weight:600;color:var(--text);">${escapeHTML(formatted.used)}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">Service Limit</span>
                <span class="mono" style="font-size:1rem;font-weight:600;color:var(--text);">${isUnlimited ? '∞' : escapeHTML(formatted.limit)} ${escapeHTML(formatted.unit)}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">Resets At</span>
                <span class="mono" style="font-size:1rem;font-weight:600;color:var(--text);">${resetTime}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">Data Source</span>
                <span class="mono" style="font-size:1rem;font-weight:700;color:var(--accent);">● ${sourceLabel}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">Account</span>
                <span class="mono truncate" style="font-size:1rem;font-weight:600;color:var(--text);" title="${escapeHTML(item.account_label || 'Default')}">${escapeHTML(item.account_label || 'Default')}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">Last Updated</span>
                <span class="mono" style="font-size:1rem;font-weight:600;color:var(--text);">${updatedTime}</span>
            </div>
        </div>

        ${item.detail ? `
        <div class="mt-5 p-4" style="background:var(--surface-2);border:1px solid var(--hairline);">
            <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;display:block;margin-bottom:10px;">Diagnostic Summary</span>
            <div class="space-y-3">
                <div class="flex flex-col gap-1">
                    <span style="font-size:10px;font-weight:700;color:var(--text-muted);text-transform:uppercase;">Backend Source</span>
                    <p class="mono leading-relaxed" style="font-size:11px;color:var(--text-muted);">${escapeHTML(item.detail)}</p>
                </div>
                <div class="grid grid-cols-2 gap-3 pt-2" style="border-top:1px solid var(--hairline);">
                    <div class="flex flex-col gap-0.5">
                        <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;">Provider Key</span>
                        <span class="mono" style="font-size:11px;color:var(--text-muted);">${escapeHTML(item.provider_id || 'unknown')}</span>
                    </div>
                    <div class="flex flex-col gap-0.5">
                        <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;">Snapshot ID</span>
                        <span class="mono" style="font-size:11px;color:var(--text-muted);">${escapeHTML(item.account_id || 'default')}</span>
                    </div>
                </div>
                <div class="pt-2">
                    <p style="font-size:9px;color:var(--text-dim);font-style:italic;line-height:1.5;">This data is cached in the local SQLite database and synchronized with the in-memory registry for instant delivery.</p>
                </div>
            </div>
            ${item.detail.includes('App-Bound Encryption') ? `
                <div class="mt-3 p-2" style="background:color-mix(in srgb,var(--accent-cool) 8%,transparent);border:1px solid var(--accent-cool);">
                    <p style="font-size:10px;color:var(--accent-cool);">💡 <strong>Tip:</strong> Chrome 127+ uses App-Bound Encryption. Try using <strong>Safari</strong> or set credentials via environment variables.</p>
                </div>
            ` : ''}
        </div>
        ` : ''}

        ${item.pace ? `
        <div class="mt-4 flex items-center justify-between px-1">
            <span style="font-size:10px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">Consumption Rate</span>
            <span class="mono" style="font-size:11px;font-weight:700;color:var(--text-muted);">${escapeHTML(item.pace)}</span>
        </div>
        ` : ''}

        ${retryButton}

        ${(item.service_name.toLowerCase().includes('github') || item.service_name.toLowerCase().includes('copilot')) ? `
        <div class="mt-5 p-4 flex flex-col gap-3" style="background:var(--surface-2);border:1px solid var(--hairline);">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                    <span style="font-size:1rem;">🐙</span>
                    <span style="font-size:11px;font-weight:700;color:var(--text);text-transform:uppercase;letter-spacing:0.06em;">GitHub Authentication</span>
                </div>
                ${STATE.githubAuth.authenticated ? `
                    <span style="font-size:9px;font-weight:700;padding:2px 8px;border:1px solid var(--good);color:var(--good);background:color-mix(in srgb,var(--good) 8%,transparent);text-transform:uppercase;letter-spacing:0.08em;">Connected</span>
                ` : `
                    <span style="font-size:9px;font-weight:700;padding:2px 8px;border:1px solid var(--hairline-strong);color:var(--text-dim);background:var(--surface-2);text-transform:uppercase;letter-spacing:0.08em;">Disconnected</span>
                `}
            </div>

            ${STATE.githubAuth.authenticated ? `
                <div class="flex items-center justify-between gap-4">
                    <div class="flex flex-col">
                        <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">Account</span>
                        <span class="mono" style="font-size:0.85rem;color:var(--text-muted);">${escapeHTML(STATE.githubAuth.account || STATE.githubAuth.name || 'Account')}${STATE.githubAuth.email ? ` <span style="color:var(--text-dim);">(${escapeHTML(STATE.githubAuth.email)})</span>` : ''}</span>
                    </div>
                    <button
                        onclick="event.stopPropagation(); window.handleGitHubLogout()"
                        style="padding:6px 14px;background:var(--surface);border:1px solid var(--hairline);color:var(--text-muted);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;"
                        class="transition-all">Logout</button>
                </div>
            ` : `
                <div class="flex flex-col gap-3">
                    <p style="font-size:11px;color:var(--text-dim);line-height:1.5;">Connect your GitHub account to fetch live Copilot usage limits directly from the GitHub API.</p>
                    <button
                        onclick="event.stopPropagation(); window.startGitHubLogin()"
                        style="width:100%;padding:10px;background:color-mix(in srgb,var(--accent-cool) 12%,transparent);border:1px solid var(--accent-cool);color:var(--accent-cool);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;"
                        class="transition-all">Connect GitHub</button>
                </div>
            `}
        </div>
        ` : ''}

    `;
}

/**
 * Build HTML for the GitHub OAuth login modal
 * @param {Object} data - Device flow data (user_code, verification_uri)
 * @param {string} [error] - Optional error message
 * @returns {string} HTML string for modal content
 */
export function buildGitHubOAuthModal(data, error = null) {
    if (error) {
        return `
            <div class="p-6 text-center">
                <div style="width:56px;height:56px;border:1px solid var(--crit);color:var(--crit);display:flex;align-items:center;justify-content:center;margin:0 auto 16px;">
                    <svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>
                </div>
                <h2 style="font-size:1rem;font-weight:700;color:var(--text);margin-bottom:8px;">CONNECTION FAILED</h2>
                <p style="font-size:12px;color:var(--text-muted);margin-bottom:20px;">${escapeHTML(error)}</p>
                <button id="close-modal" style="width:100%;padding:10px;background:var(--surface-2);border:1px solid var(--hairline);color:var(--text-muted);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;" class="transition-all">CLOSE</button>
            </div>
        `;
    }

    if (!data) {
        return `
            <div class="p-12 text-center">
                <div style="display:inline-block;width:28px;height:28px;border:2px solid color-mix(in srgb,var(--accent-cool) 30%,transparent);border-top-color:var(--accent-cool);border-radius:50%;margin-bottom:16px;" class="animate-spin"></div>
                <p style="font-size:10px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.12em;">Initializing GitHub Login...</p>
            </div>
        `;
    }

    return `
        <div class="p-2">
            <div class="flex items-center justify-between mb-8">
                <div class="flex items-center gap-3">
                    <div style="width:36px;height:36px;border:1px solid var(--accent);color:var(--accent);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:1.1rem;">🐙</div>
                    <h2 style="font-size:1rem;font-weight:700;color:var(--text);letter-spacing:0.04em;text-transform:uppercase;">Connect GitHub</h2>
                </div>
                <button id="close-modal" class="icon-btn w-10 h-10 flex items-center justify-center transition-colors" style="color:var(--text-muted);">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                </button>
            </div>

            <div class="space-y-6">
                <div class="text-center">
                    <p style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">Enter this code on GitHub to authorize Runway:</p>
                    <div class="mono select-all" style="font-size:2rem;font-weight:700;letter-spacing:0.25em;color:var(--accent);padding:16px;border:1px solid var(--accent);background:color-mix(in srgb,var(--accent) 5%,transparent);margin:12px 0;">
                        ${escapeHTML(data.user_code)}
                    </div>
                </div>

                <div style="background:var(--surface-2);padding:16px;border:1px solid var(--hairline);">
                    <ol class="space-y-3" style="font-size:12px;color:var(--text-muted);">
                        <li class="flex gap-3">
                            <span style="flex-shrink:0;width:18px;height:18px;border:1px solid var(--hairline-strong);color:var(--text-dim);display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;">1</span>
                            <span>Open <a href="${safeUrl(data.verification_uri)}" target="_blank" style="color:var(--accent-cool);font-weight:700;" class="hover:underline">${escapeHTML(data.verification_uri)}</a></span>
                        </li>
                        <li class="flex gap-3">
                            <span style="flex-shrink:0;width:18px;height:18px;border:1px solid var(--hairline-strong);color:var(--text-dim);display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;">2</span>
                            <span>Enter the 8-character code shown above.</span>
                        </li>
                        <li class="flex gap-3">
                            <span style="flex-shrink:0;width:18px;height:18px;border:1px solid var(--hairline-strong);color:var(--text-dim);display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;">3</span>
                            <span>Once authorized, this window will close automatically.</span>
                        </li>
                    </ol>
                </div>

                <div class="flex items-center justify-center gap-3 py-2">
                    <div class="flex gap-1.5">
                        <div style="width:5px;height:5px;background:var(--accent-cool);border-radius:50%;" class="animate-bounce [animation-delay:-0.3s]"></div>
                        <div style="width:5px;height:5px;background:var(--accent-cool);border-radius:50%;" class="animate-bounce [animation-delay:-0.15s]"></div>
                        <div style="width:5px;height:5px;background:var(--accent-cool);border-radius:50%;" class="animate-bounce"></div>
                    </div>
                    <span style="font-size:10px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.1em;">Waiting for authorization...</span>
                </div>

                <button id="cancel-github-login" style="width:100%;padding:10px;background:var(--surface-2);border:1px solid var(--hairline);color:var(--text-muted);font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:0.1em;" class="transition-all">CANCEL</button>
            </div>
        </div>
    `;
}

/**
 * Build HTML for the token health panel in Settings
 * @param {Array} tokens - Array of token health objects from /api/v1/system/token-health
 * @returns {string} HTML string for the panel
 */
export function buildTokenHealthPanel(tokens) {
    if (!tokens || tokens.length === 0) {
        return `<div class="mt-8 pt-6" style="border-top:1px solid var(--hairline);">
            <h3 style="font-size:11px;font-weight:700;color:var(--text-muted);margin-bottom:10px;text-transform:uppercase;letter-spacing:0.1em;" class="flex items-center gap-2"><span>🔑</span> Token Health</h3>
            <p style="font-size:11px;color:var(--text-dim);font-style:italic;">No active credentials in cache.</p>
        </div>`;
    }

    const STATUS_STYLES = {
        valid:    { color: 'var(--good)',   label: 'READY' },
        expiring: { color: 'var(--warn)',   label: 'EXPIRING' },
        expired:  { color: 'var(--crit)',   label: 'EXPIRED' },
        unknown:  { color: 'var(--text-dim)', label: 'UNKNOWN' },
    };

    const FRIENDLY_TYPES = {
        'api_key': 'API Key',
        'session_cookie': 'Cookie',
        'oauth_token': 'OAuth',
        'access_token': 'Token',
        'id_token': 'ID Token',
        'refresh_token': 'Refresh'
    };

    const rows = tokens.map(t => {
        const s = STATUS_STYLES[t.status] || STATUS_STYLES.unknown;
        
        // Suppress redundant "config" and "default" labels if we have a settings badge or no custom label
        let label = t.account_label || t.account_id || '';
        if (label === 'default' || (t.source === 'config' && (label === 'config' || label === 'config-cookie'))) {
            label = '';
        }

        const rawTypes = t.token_types || [];
        const seenTypes = new Set();
        const types = [];

        for (const k of rawTypes) {
            let clean = FRIENDLY_TYPES[k] || k;
            
            // Consolidate browser-scraped multi-cookie bundles into a single "Cookie" badge
            if (k.startsWith('COOKIE_') || k.startsWith('__Secure-') || k.toLowerCase().includes('session')) {
                clean = 'Cookie';
            }

            if (!seenTypes.has(clean)) {
                types.push(`<span class="tag-pill">${escapeHTML(clean)}</span>`);
                seenTypes.add(clean);
            }
        }
        const typesHTML = types.join('');
        const expiryStr = t.expires_at
            ? formatLocalDateTime(t.expires_at)
            : t.ttl_remaining_seconds > 0
            ? `cache TTL ${t.ttl_remaining_seconds}s`
            : '';

        const refreshBtn = (t.can_refresh && t.status !== 'valid') ? `
            <button onclick="window.refreshToken('${escapeHTMLAttr(t.provider)}', '${escapeHTMLAttr(t.account_id)}')"
                    style="font-size:9px;font-weight:700;padding:2px 8px;border:1px solid var(--unlm);color:var(--unlm);background:color-mix(in srgb,var(--unlm) 8%,transparent);text-transform:uppercase;letter-spacing:0.06em;"
                    class="transition-all">
                REFRESH
            </button>` : '';

        const purgeBtn = `
            <button onclick="window.deleteToken('${escapeHTMLAttr(t.provider)}', '${escapeHTMLAttr(t.account_id)}')"
                    class="icon-btn w-7 h-7 flex items-center justify-center transition-all ml-1"
                    style="color:var(--text-dim);"
                    title="Purge from cache">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4h6v2"></path></svg>
            </button>`;

        const sourceBadge = t.source === 'config'
            ? `<span class="mono" style="font-size:9px;padding:2px 6px;border:1px solid var(--accent);color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,transparent);" title="Configured in Settings → Providers">⚙ settings</span>`
            : t.source
            ? `<span class="mono" style="font-size:9px;padding:2px 6px;border:1px solid var(--unlm);color:var(--unlm);background:color-mix(in srgb,var(--unlm) 8%,transparent);" title="Delivered by sidecar">⬡ ${escapeHTML(t.source)}</span>`
            : `<span class="mono" style="font-size:9px;padding:2px 6px;border:1px solid var(--hairline-strong);color:var(--text-dim);background:var(--surface-2);" title="Collected locally">local</span>`;

        return `
        <div class="flex items-center justify-between gap-2 py-3" style="border-bottom:1px solid var(--hairline);">
            <div class="flex flex-col min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                    <span class="mono" style="font-size:11px;font-weight:700;color:var(--text);">${escapeHTML(t.provider)}</span>
                    <span style="font-size:10px;color:var(--text-muted);">${escapeHTML(label)}</span>
                    ${sourceBadge}
                </div>
                <div class="flex flex-wrap gap-1 mt-1">${typesHTML}</div>
                ${expiryStr ? `<span class="mono mt-0.5" style="font-size:10px;color:var(--text-dim);">${escapeHTML(expiryStr)}</span>` : ''}
            </div>
            <div class="flex items-center gap-1 shrink-0">
                ${refreshBtn}
                ${purgeBtn}
                <span style="font-size:9px;font-weight:700;padding:2px 8px;border:1px solid ${s.color};color:${s.color};background:color-mix(in srgb,${s.color} 8%,transparent);text-transform:uppercase;letter-spacing:0.08em;">${s.label}</span>
            </div>
        </div>`;
    }).join('');

    return `<div class="mt-8 pt-6" style="border-top:1px solid var(--hairline);">
        <h3 style="font-size:11px;font-weight:700;color:var(--text-muted);margin-bottom:10px;text-transform:uppercase;letter-spacing:0.1em;" class="flex items-center gap-2"><span>🔑</span> Token Health</h3>
        <div>${rows}</div>
    </div>`;
}

/**
 * Build HTML for the fleet view showing all registered sidecars
 * @param {Array} sidecars - Array of sidecar registry objects
 * @returns {string} HTML string for the fleet view
 */
export function buildFleetView(sidecars) {
    if (!sidecars || sidecars.length === 0) {
        return `<div class="empty-state text-center py-16 text-zinc-600 text-sm">No sidecars registered yet. Start a sidecar to see it here.</div>`;
    }

    const rows = sidecars.map(s => {
        const now = Date.now();
        const lastSeen = s.last_seen ? new Date(s.last_seen) : null;
        const ageMs = lastSeen ? now - lastSeen.getTime() : Infinity;
        let lampClass, dotTitle;
        if (ageMs < 30 * 60 * 1000) {
            lampClass = 'lamp-good'; dotTitle = 'Active (seen < 30m ago)';
        } else if (ageMs < 2 * 60 * 60 * 1000) {
            lampClass = 'lamp-warn'; dotTitle = 'Idle (seen < 2h ago)';
        } else {
            lampClass = 'lamp-unk'; dotTitle = 'Stale';
        }

        const displayName = s.custom_name || s.hostname || s.sidecar_id;
        const tags = (s.tags || []).map(t => `<span class="tag-pill">${escapeHTML(t)}</span>`).join('');
        const lastSeenStr = lastSeen ? formatLocalDateTime(s.last_seen) : '—';
        const staleBanner = s.stale
            ? `<div style="display:flex;align-items:center;gap:6px;padding:6px 8px;border:1px solid var(--warn);background:color-mix(in srgb,var(--warn) 8%,transparent);font-size:10px;color:var(--warn);">
                   <span class="lamp lamp-warn" style="flex-shrink:0;"></span>
                   No check-in for over ${s.stale_threshold_minutes ?? 60} minutes
               </div>`
            : '';

        return `
        <div class="glass-panel p-5 flex flex-col gap-3" data-sidecar="${escapeHTML(s.sidecar_id)}">
            <div class="flex items-start justify-between gap-2">
                <div class="flex items-center gap-2 min-w-0">
                    <div class="lamp ${lampClass}" title="${dotTitle}"></div>
                    <div class="flex flex-col min-w-0">
                        <span class="fleet-name truncate cursor-pointer transition-colors"
                              style="font-size:0.85rem;font-weight:700;color:var(--text);"
                              onclick="window.editSidecarName('${escapeHTMLAttr(s.sidecar_id)}')"
                              title="Click to rename">${escapeHTML(displayName)}</span>
                        <span class="mono truncate" style="font-size:10px;color:var(--text-dim);">${escapeHTML(s.sidecar_id)}</span>
                    </div>
                </div>
                <div class="flex items-center gap-1 shrink-0">
                    ${(() => {
                        const enabled = s.collection_enabled !== false;
                        const iconSvg = enabled
                            ? '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>'
                            : '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>';
                        const title = enabled
                            ? 'Pause collection on this sidecar'
                            : 'Resume collection on this sidecar';
                        const color = enabled ? 'var(--text-dim)' : 'var(--accent)';
                        return `<button onclick="window.toggleSidecarEnabled('${escapeHTMLAttr(s.sidecar_id)}', ${enabled})"
                                data-sidecar-toggle
                                class="icon-btn w-7 h-7 flex items-center justify-center transition-all"
                                style="color:${color};"
                                title="${title}">${iconSvg}</button>`;
                    })()}
                    <button onclick="window.deleteSidecar('${escapeHTMLAttr(s.sidecar_id)}')"
                            class="icon-btn w-7 h-7 flex items-center justify-center transition-all"
                            style="color:var(--text-dim);"
                            title="Remove sidecar">
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4h6v2"></path></svg>
                    </button>
                </div>
            </div>
            ${staleBanner}
            <div class="flex flex-wrap gap-1 items-center">
                ${tags}
                <button onclick="window.addSidecarTag('${escapeHTMLAttr(s.sidecar_id)}')"
                        style="font-size:9px;font-weight:700;color:var(--text-dim);padding:2px 6px;border:1px dashed var(--hairline-strong);"
                        class="transition-all hover:border-accent">+ TAG</button>
            </div>
            <div class="pt-2 grid grid-cols-2 gap-2 mono" style="border-top:1px solid var(--hairline);font-size:10px;color:var(--text-muted);">
                <div><span style="color:var(--text-dim);">LAST SEEN</span><br/>${escapeHTML(lastSeenStr)}</div>
                <div><span style="color:var(--text-dim);">INGESTS</span><br/>${s.ingest_count ?? 0}</div>
                <div><span style="color:var(--text-dim);">IP</span><br/>${escapeHTML(s.last_ip || '—')}</div>
                <div><span style="color:var(--text-dim);">ERRORS</span><br/>${s.error_count ?? 0}</div>
                <div><span style="color:var(--text-dim);">VERSION</span><br/>${escapeHTML(s.sidecar_version || '—')}${
                    s.update_available
                        ? ` <span style="margin-left:4px;padding:1px 5px;background:color-mix(in srgb,var(--warn) 15%,transparent);border:1px solid var(--warn);color:var(--warn);font-size:9px;font-weight:700;letter-spacing:0.05em;" title="Update available: ${escapeHTMLAttr(s.latest_version || '')}">↑ UPDATE</span>`
                        : ''
                }</div>
                <div><span style="color:var(--text-dim);">OS</span><br/>${escapeHTML(s.os_platform || '—')}</div>
            </div>
            ${(s.recent_logs && s.recent_logs.length > 0) ? `
            <details class="mt-0.5">
                <summary style="font-size:9px;color:var(--text-dim);cursor:pointer;" class="select-none list-none flex items-center gap-1 transition-colors hover:text-text">
                    <svg class="details-arrow" xmlns="http://www.w3.org/2000/svg" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
                    Recent logs (${s.recent_logs.length} lines)
                </summary>
                <pre class="mt-1.5 mono overflow-x-hidden overflow-y-auto whitespace-pre-wrap break-all max-h-32 leading-relaxed" style="font-size:9px;color:var(--text-muted);background:var(--surface-2);padding:8px;border-radius:1px;">${s.recent_logs.map(l => escapeHTML(l)).join('\n')}</pre>
            </details>` : ''}
        </div>`;
    }).join('');

    return `<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">${rows}</div>`;
}


/** Health severity for sorting (higher = worse) */
const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };


/**
 * Build a minimal SVG sparkline from a series of {value} points.
 * @param {Array<{value: number}>} points - ordered oldest→newest
 * @param {string} color - hex color
 * @param {number} [width=64]
 * @param {number} [height=28]
 * @returns {string} SVG HTML string
 */
function buildSparklineSVG(points, color, width = 64, height = 28) {
    if (!points || points.length < 2) {
        return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"></svg>`;
    }
    const values = points.map(p => p.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const pad = 3;

    const svgPoints = points.map((p, i) => {
        const x = ((i / (points.length - 1)) * (width - pad * 2) + pad).toFixed(1);
        const y = (height - pad - ((p.value - min) / range) * (height - pad * 2)).toFixed(1);
        return `${x},${y}`;
    }).join(' ');

    const [lastX, lastY] = svgPoints.split(' ').pop().split(',');

    return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" style="overflow:visible;">
        <polyline points="${svgPoints}" fill="none" stroke="${color}" stroke-width="1.5"
            stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${lastX}" cy="${lastY}" r="2.5" fill="${color}"/>
    </svg>`;
}

/**
 * Derive trend arrow from a points series.
 * @param {Array<{value: number}>} points
 * @returns {'↑'|'↓'|'→'}
 */
function getTrendArrow(points) {
    if (!points || points.length < 2) return '→';
    const delta = points[points.length - 1].value - points[0].value;
    if (delta > 3) return '↑';
    if (delta < -3) return '↓';
    return '→';
}

/**
 * Build skeleton loading state for the provider modal.
 * @param {number} count - number of service rows to show skeletons for
 * @returns {string} HTML string
 */
export function buildModalSkeleton(count) {
    const rows = Array(Math.min(count, 10)).fill(0).map(() => `
        <div style="background:var(--surface);border:1px solid var(--hairline);padding:16px;border-radius:1px;">
            <div class="flex justify-between items-start mb-2.5">
                <div class="flex-1 min-w-0">
                    <div class="skeleton h-6 w-32 mb-2" style="border-radius:1px;"></div>
                    <div class="flex gap-2">
                        <div class="skeleton h-4 w-16" style="border-radius:1px;"></div>
                        <div class="skeleton h-4 w-12" style="border-radius:1px;"></div>
                    </div>
                </div>
                <div class="skeleton w-16 h-7" style="border-radius:1px;"></div>
            </div>
            <div class="skeleton h-5 w-full mb-2" style="border-radius:1px;"></div>
            <div class="h-1 overflow-hidden" style="background:var(--surface-2);">
                <div class="skeleton h-full w-3/4" style="border-radius:0;"></div>
            </div>
        </div>
    `).join('');
    
    return rows;
}

/**
 * Build the provider drill-down modal.
 * @param {string} providerId
 * @param {Array} items - LimitCard items for this provider (sorted worst-first)
 * @param {Array} history - raw history snapshots from /api/v1/usage/history
 * @returns {string} HTML string
 */
export function buildProviderModal(providerId, items, history) {
    const icon = PROVIDER_ICONS[providerId] || '🔧';
    const accounts = [...new Set(items.map(i => i.account_label).filter(Boolean))];
    const accountText = accounts.join(' · ') || '';
    const windowType = items[0]?.window_type || '';
    const serviceCount = items.length;

    // Preserve the order passed in — openProviderModal already applies the user's layout.
    const sorted = items;

    const BAR_HEX = { critical: 'var(--crit)', warning: 'var(--warn)', good: 'var(--good)', unlimited: 'var(--unlm)', unknown: 'var(--unk)' };
    const MODAL_SOURCE_LABELS = { 
        oauth: 'OAuth', 
        web_api: 'Web API', 
        scrape: 'Scrape', 
        logs: 'Logs', 
        statusline: 'Statusline', 
        api: 'API', 
        sidecar: 'Sidecar',
        cache: 'Cache',
        fallback: 'Fallback'
    };
    const MODAL_INPUT_LABELS = {
        sidecar: 'Sidecar',
        config: 'Config',
        server: 'Server'
    };

    const serviceRows = sorted.map(item => {
        const h = HEALTH_CONFIG[item.health] || HEALTH_CONFIG.unknown;
        const barColor = BAR_HEX[item.health] || '#3f3f46';
        const badgeLabels = { critical: 'CRIT', warning: 'WARN', good: 'GOOD', unlimited: 'UNLM', unknown: '——' };

        // Percentage
        let pct = null;
        if (!item.is_unlimited && item.used_value != null && item.limit_value > 0) {
            pct = (item.used_value / item.limit_value) * 100;
        }
        const barWidth = item.is_unlimited ? 100 : (pct ?? 0);

        // Sparkline — filter history for this service+window so two windows of the
        // same service don't bleed into one mixed sparkline.
        const svcHistory = (history || [])
            .filter(s => s.provider_id === providerId
                && s.service_name === item.service_name
                && (s.variant || null) === (item.variant || null)
                && (s.window_type || null) === (item.window_type || null)
                && (s.model_id || null) === (item.model_id || null)
                && typeof s.used_value === 'number' && isFinite(s.used_value))
            .sort((a, b) => a.timestamp.localeCompare(b.timestamp))
            .map(s => ({ value: s.used_value }));
        const sparkColor = item.is_unlimited ? 'var(--unlm)' : barColor;
        const sparkSVG = buildSparklineSVG(svcHistory, sparkColor);
        const trendArrow = getTrendArrow(svcHistory);

        // Used / limit display
        const fmt = formatUsageValues(item.used_value, item.limit_value, item.unit_type, item.currency);
        const usageText = item.is_unlimited
            ? (item.unit_type === 'tokens' ? (item.used_value / 1000000).toFixed(1) + 'M tokens' : 'Unlimited')
            : (fmt.used !== '—' && fmt.limit !== '—')
            ? `${fmt.used} / ${fmt.limit}${fmt.unit ? ' ' + fmt.unit : ''}`
            : escapeHTML(String(item.remaining ?? '—'));

        const resetText = item.reset_at ? escapeHTML(formatResetDisplay(item.reset_at)) : escapeHTML(String(item.reset ?? '—'));
        const sourceLabel = MODAL_SOURCE_LABELS[item.data_source] || escapeHTML(item.data_source || '');
        const inputLabel = MODAL_INPUT_LABELS[item.input_source] || escapeHTML(item.input_source || '');
        
        const combinedSourceLabel = item.input_source && item.input_source !== 'unknown' 
            ? `${sourceLabel} · ${inputLabel}` 
            : sourceLabel;

        const paceIcon = getPaceIcon(item.pace);
        const tierBadge = item.tier ? getTierBadge(item.tier) : '';

        return `<div style="background:var(--surface);border:1px solid var(--hairline);padding:16px;border-radius:1px;position:relative;" data-card-key="${escapeHTMLAttr(cardKey(item))}">
            <span class="drag-handle" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                    <circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/>
                    <circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/>
                    <circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/>
                </svg>
            </span>
            <div class="flex justify-between items-start mb-2.5">
                <div class="flex-1 min-w-0">
                    <div style="font-size:1.1rem;font-weight:700;color:var(--text);">${escapeHTML(item.service_name)}</div>
                    ${_windowSubtitle(item)}
                    <div class="flex flex-wrap items-center gap-1.5" style="margin-top:6px;">
                        <span class="tag ${h.tag}">${badgeLabels[item.health] || '——'}</span>
                        ${tierBadge}
                        ${combinedSourceLabel ? `<span style="font-size:10px;color:var(--text-muted);">${combinedSourceLabel}</span>` : ''}
                        ${paceIcon ? `<span style="font-size:1rem;">${paceIcon}</span>` : ''}
                        ${item.pace ? `<span style="font-size:10px;color:var(--text-muted);">${escapeHTML(item.pace)}</span>` : ''}
                    </div>
                </div>
                <div class="flex items-center gap-2 flex-shrink-0 ml-3">
                    <span style="font-size:1rem;color:var(--text-dim);">${trendArrow}</span>
                    ${sparkSVG}
                </div>
            </div>
            <div class="flex justify-between mb-2" style="font-size:0.9rem;color:var(--text-muted);">
                <span>${usageText}</span>
                <span style="color:var(--text-dim);">${resetText}</span>
            </div>
            <div class="h-1 overflow-hidden" style="background:var(--surface-2);">
                <div class="h-full transition-all" style="width:${Math.min(barWidth, 100).toFixed(1)}%;background:${barColor};border-radius:0;"></div>
            </div>
        </div>`;
    }).join('');

    // RAW payload for debugging
    const rawPayload = JSON.stringify(items, null, 2);

    return `<div>
        <div class="flex justify-between items-start mb-5 pb-4" style="border-bottom:1px solid var(--hairline);">
            <div>
                <div style="font-size:1.1rem;font-weight:700;color:var(--text);letter-spacing:0.04em;">${icon} ${escapeHTML(providerId)}</div>
                <div style="font-size:11px;color:var(--text-muted);margin-top:4px;">${escapeHTML(accountText)}${windowType ? ' · ' + escapeHTML(windowType) : ''} · ${serviceCount} service${serviceCount !== 1 ? 's' : ''}</div>
            </div>
            <div class="flex items-center gap-1">
                <button id="refresh-provider-btn" title="Refresh now" class="icon-btn w-8 h-8 flex items-center justify-center transition-colors" style="color:var(--text-muted);">
                    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
                </button>
                <button id="close-modal" class="icon-btn transition-colors text-xl leading-none w-8 h-8 flex items-center justify-center" style="color:var(--text-muted);">✕</button>
            </div>
        </div>
        <div class="space-y-3 max-h-[55vh] overflow-y-auto pr-1" data-provider-id="${escapeHTMLAttr(providerId)}">${serviceRows}</div>
        <!-- Actions footer -->
        <div class="flex items-center gap-2 mt-5 pt-4 flex-wrap" style="border-top:1px solid var(--hairline);">
            <button class="toggle-btn" onclick="openProviderInHistory('${escapeHTMLAttr(providerId)}')">Open in History</button>
        </div>
        <!-- RAW PAYLOAD -->
        <details class="mt-4" style="font-size:10px;">
            <summary style="cursor:pointer;color:var(--text-dim);letter-spacing:0.08em;text-transform:uppercase;list-style:none;display:flex;align-items:center;gap:6px;">
                <span style="font-size:9px;border:1px solid var(--hairline-strong);padding:1px 5px;">▶</span> RAW PAYLOAD
            </summary>
            <pre style="margin-top:8px;padding:12px;background:var(--surface-2);border:1px solid var(--hairline);overflow-x:auto;font-size:10px;color:var(--text-muted);max-height:300px;overflow-y:auto;line-height:1.5;">${escapeHTML(rawPayload)}</pre>
        </details>
    </div>`;
}

/**
 * Build the per-provider sparkline summary strip for the History tab.
 * @param {Array} history - raw history snapshots
 * @param {Set|null} activeProviders - Set of active provider IDs (null = all active)
 * @returns {string} HTML string
 */
export function buildProviderSparklineStrip(history, activeProviders, days = 7) {
    if (!history || history.length === 0) return '';

    // Group history by provider
    const byProvider = new Map();
    for (const s of history) {
        if (typeof s.used_value !== 'number' || !isFinite(s.used_value)) continue;
        const pid = s.provider_id || 'unknown';
        if (!byProvider.has(pid)) byProvider.set(pid, []);
        byProvider.get(pid).push(s);
    }

    if (byProvider.size === 0) return '';

    const PROVIDER_HEX = {
        anthropic: '#f59e0b', gemini: '#3b82f6', github: '#8b5cf6',
        chatgpt: '#10b981', opencode: '#06b6d4', openrouter: '#ec4899',
        minimax: '#14b8a6', ollama: '#94a3b8',
    };

    const bucketSeconds = pickBucketSeconds(days);

    const cards = [...byProvider.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([pid, snaps]) => {
        // Build series summed per bucket so the sparkline shape tracks per-bucket totals,
        // and the headline number is the sum across all buckets (period total).
        const byBucket = new Map();
        for (const s of snaps) {
            const t = Math.floor(new Date(s.timestamp).getTime() / 1000);
            const bucket = t - (t % bucketSeconds);
            byBucket.set(bucket, (byBucket.get(bucket) || 0) + s.used_value);
        }
        const points = [...byBucket.entries()]
            .sort(([a], [b]) => a - b)
            .map(([, v]) => ({ value: v }));

        const periodTotal = points.reduce((acc, p) => acc + p.value, 0);
        // Skip providers with zero usage in the active range — keeps the strip focused on what's live.
        if (periodTotal <= 0) return '';

        const icon = PROVIDER_ICONS[pid] || '🔧';
        const color = PROVIDER_HEX[pid] || '#64748b';
        const isActive = !activeProviders || activeProviders.has(pid);

        const sparkSVG = buildSparklineSVG(points, color, 56, 24);
        const trendArrow = getTrendArrow(points);
        const latestValue = Math.round(periodTotal).toLocaleString();
        const trendStyle = trendArrow === '↑' ? 'color:var(--crit);' : trendArrow === '↓' ? 'color:var(--good);' : 'color:var(--text-dim);';
        const activeOpacity = isActive ? '' : 'opacity-40';

        return `<div class="glass-panel cursor-pointer select-none hover:opacity-90 transition-all ${activeOpacity}"
                     style="border:1px solid ${isActive ? color : 'var(--hairline)'};padding:10px;"
                     onclick="toggleHistoryProvider('${escapeHTMLAttr(pid)}')">
            <div class="flex items-center justify-between gap-2 mb-1.5">
                <span style="font-size:9px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.08em;">${icon} ${escapeHTML(pid)}</span>
                ${sparkSVG}
            </div>
            <div class="flex items-baseline gap-1">
                <span style="font-size:0.85rem;font-weight:700;color:var(--text);">${latestValue}</span>
                <span style="font-size:10px;font-weight:700;${trendStyle}">${trendArrow}</span>
            </div>
        </div>`;
    }).join('');

    return `<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-4">${cards}</div>`;
}

/* ============================================================
   Horizon Card v2 + Card Modal  (Dashboard Phase 1)
   ============================================================ */


function _hzHealthClass(health) {
    const m = { good: 'h-good', warning: 'h-warn', critical: 'h-crit', unknown: 'h-unk', unlimited: 'h-unlm' };
    return m[health] || '';
}

function _srcBadgeClass(dataSource) {
    if (!dataSource) return '';
    const s = String(dataSource).toLowerCase();
    if (s === 'api' || s === 'oauth') return ' src-oauth';
    if (s === 'web')                  return ' src-web';
    if (s === 'local')                return ' src-log';
    if (s === 'sidecar')              return ' src-sidecar';
    return '';
}

function _tierBadgeClass(tier) {
    if (!tier) return '';
    const s = String(tier).toLowerCase().split(' ')[0];
    if (s === 'free')                                     return ' tier-free';
    if (s === 'max')                                      return ' tier-max';
    if (s === 'pro' || s === 'plus' || s === 'premium' || s === 'individual' || s === 'go') return ' tier-pro';
    return '';
}

function _windowSubtitle(card) {
    const text = cardSubtitleText(card);
    if (!text) return '';
    return `<div style="font-size:9px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.06em;line-height:1;" class="truncate">${escapeHTML(text)}</div>`;
}

function _forecastPaceBucket(forecastEntry) {
    if (!forecastEntry) return null;
    const s = forecastEntry.status;
    if (s === 'risk' || s === 'exhausted') return 'fast';
    if (s === 'warn') return 'moderate';
    if (s === 'ok'   || s === 'stable')   return 'stable';
    return null;
}

function _windowLabel(wt) {
    const m = { session: 'session', daily: 'daily', weekly: 'weekly', biweekly: 'biweekly',
                monthly: 'monthly', prepaid: 'prepaid', rolling: 'rolling', unknown: '—' };
    return m[wt] || wt || '—';
}

function _buildHzFoot(card, forecastEntry, pace, projPct, usedPct) {
    if (card.is_unlimited) return '';
    const parts = [];
    if (pace) {
        parts.push(`<div class="hz-meta"><span class="hz-lbl"><span class="pace-dot ${pace}"></span>pace</span><b>${pace}</b></div>`);
    }
    if (projPct != null) {
        parts.push(`<div class="hz-meta"><span class="hz-lbl">land at</span><b>${Math.round(projPct)}%</b></div>`);
    }
    if (forecastEntry && projPct != null) {
        const safe = projPct < 90;
        parts.push(`<div class="hz-meta"><span class="hz-lbl">status</span><b class="${safe ? 'ok' : 'risk'}">${safe ? 'safe' : 'risk'}</b></div>`);
    }
    return parts.length ? `<div class="hz-foot">${parts.join('')}</div>` : '';
}

/**
 * Build a Horizon-variant quota card for the v2 dashboard.
 * @param {Object} card  - LimitCard from /api/v1/usage/limits
 * @param {Object|null} forecastEntry - ForecastEntry from STATE.forecastMap (may be undefined)
 * @returns {string} HTML for one <article> card
 */
export function buildHorizonCard(card, forecastEntry) {
    const prov = _PROV_MAP[card.provider_id] || { key: 'default', init: (card.provider_id || '??').slice(0, 2).toUpperCase() };
    const hCls = _hzHealthClass(card.health);
    const cKey = cardKey(card);

    const tierBadge = card.tier
        ? `<span class="badge${_tierBadgeClass(card.tier)}">${escapeHTML(String(card.tier))}</span>`
        : '';
    const srcBadge = card.data_source
        ? `<span class="badge${_srcBadgeClass(card.data_source)}">${escapeHTML(card.data_source.slice(0, 7))}</span>`
        : '';

    const subParts = cardSubtitleParts(card).map(escapeHTML);
    if (card.account_label) subParts.push(escapeHTML(card.account_label));
    const head = `<div class="row">
        <div class="plogo c-${prov.key}" style="font-size:${card.icon ? '14px' : '10px'};">${escapeHTML(card.icon || prov.init)}</div>
        <div class="stack-xs">
            <span class="title">${escapeHTML(card.service_name)}</span>
            <span class="sub">${subParts.join(' · ')}</span>
        </div>
        <div class="tooltip-container" style="cursor:help;padding:2px;margin:3px -2px 0 -2px;">
            <div class="health" style="margin-top:0;"></div>
            <div class="tooltip" style="right:0;bottom:100%;margin-bottom:10px;z-index:200;">
                <div style="font-weight:700;margin-bottom:2px;">${escapeHTML((card.provider_id || '??').toUpperCase())}</div>
                <div style="font-size:10px;color:var(--text-dim);">${escapeHTML((card.health || 'unknown').charAt(0).toUpperCase() + (card.health || 'unknown').slice(1))} Status</div>
            </div>
        </div>
        <div class="badges">${tierBadge}${srcBadge}</div>
    </div>`;

    // Error card variant
    if (card.error_type) {
        const errText = card.error_type === 'rate_limited' ? (card.detail || 'Rate Limited') : 'collector error';
        return `<article class="glass card err ${hCls}" data-prov="${escapeHTMLAttr(card.provider_id || '')}" data-card-key="${escapeHTMLAttr(cKey)}">
            ${head}
            <div class="hz-head"><div class="pct" style="font-size:16px;color:var(--crit);text-transform:uppercase;letter-spacing:0.02em;">${escapeHTML(errText)}</div></div>
            <button class="retry" onclick="event.stopPropagation();window.handleResetProvider(event,'${escapeHTMLAttr(card.provider_id || '')}','${escapeHTMLAttr(card.account_id || '')}')">↺ retry</button>
        </article>`;
    }

    // Compute percentages
    const pctUsed = card.pct_used != null ? card.pct_used
        : (card.used_value != null && card.limit_value ? card.used_value / card.limit_value * 100 : null);
    const pctRemaining = pctUsed != null ? Math.max(0, Math.round(100 - pctUsed)) : null;
    const usedPct = Math.min(100, Math.round(pctUsed ?? 0));

    // Forecast stripe
    const projPct = forecastEntry?.projected_pct;
    const projWidth = projPct != null
        ? Math.max(0, Math.min(100 - usedPct, Math.round(projPct) - usedPct))
        : null;
    const pace = _forecastPaceBucket(forecastEntry);

    // Reset display
    const resetStr = card.reset_at ? formatHumanDelta(new Date(card.reset_at)) : (card.reset || '—');

    // Glide Path Calculation
    let glidePathMarker = '';
    if (!card.is_unlimited && card.reset_at && card.window_type !== 'unknown') {
        const resetDate = new Date(card.reset_at);
        const now = new Date();
        // Approximation of window durations
        const durations = { session: 5 * 3600000, daily: 86400000, weekly: 604800000, monthly: 2592000000 };
        const windowMs = durations[card.window_type] || 86400000;
        const elapsed = windowMs - (resetDate - now);
        const glidePct = Math.max(0, Math.min(100, (elapsed / windowMs) * 100));
        
        const isOverPace = usedPct > glidePct;
        const markerColor = isOverPace ? 'var(--warn)' : 'var(--good)';
        glidePathMarker = `<div class="glide-path" style="left:${glidePct}%; background:${markerColor}; width:1px; height:10px; position:absolute; top:-2px; z-index:10; box-shadow: 0 0 4px ${markerColor};"></div>`;
    }

    if (card.is_unlimited || !card.limit_value) {
        // Velocity Card Variant
        const formatted = formatUsageValues(card.used_value, card.limit_value, card.unit_type, card.currency);
        return `<article class="glass card v-horizon ${hCls}" data-prov="${escapeHTMLAttr(card.provider_id || '')}" data-card-key="${escapeHTMLAttr(cKey)}">
            ${head}
            <div class="hz-head">
                <div class="pct" style="color:var(--unlm); font-size:24px;">${escapeHTML(formatted.used)}</div>
                <span class="sub">Total Spend</span>
                <div class="reset">Burn: <b>${pace || 'stable'}</b></div>
            </div>
            <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.04em;text-transform:uppercase;margin-top:4px;">
                ${card.token_usage?.total ? `Total Tokens: ${_formatTokenShort(card.token_usage.total)}` : '&nbsp;'}
            </div>
            <div class="hz-foot" style="border-top:1px dashed var(--hairline); margin-top:8px; padding-top:4px;">
                <div class="hz-meta"><span class="hz-lbl">forecast</span><b>${projPct ? Math.round(projPct) + '%' : '—'}</b></div>
            </div>
        </article>`;
    }

    const hzHeadContent = pctRemaining != null
        ? `<div class="pct">${pctRemaining}<em>%</em></div><span class="sub">remaining</span>`
        : `<div class="pct" style="color:var(--unk);">—</div>`;

    const hzBar = `<div class="horizon">
        <div class="used" style="width:${usedPct}%"></div>
        ${projWidth != null && projWidth > 0 ? `<div class="projected" style="left:${usedPct}%;width:${projWidth}%"></div>` : ''}
        <div class="now" style="left:${usedPct}%"></div>
        ${glidePathMarker}
        <div class="reset-mk"></div>
        <div class="axis"><span>NOW</span><span>+1h</span><span>+2h</span><span>RESET</span></div>
    </div>`;

    const tokenDetails = card.token_usage?.total 
        ? `<div style="font-size:10px;color:var(--text-dim);letter-spacing:0.04em;text-transform:uppercase;margin-top:4px;">Total Tokens: ${_formatTokenShort(card.token_usage.total)}</div>`
        : `<div style="height:18px;"></div>`;

    return `<article class="glass card v-horizon ${hCls}" data-prov="${escapeHTMLAttr(card.provider_id || '')}" data-card-key="${escapeHTMLAttr(cKey)}">
        ${head}
        <div class="hz-head">${hzHeadContent}<div class="reset">resets <b>${escapeHTML(resetStr)}</b></div></div>
        ${tokenDetails}
        ${hzBar}
        ${_buildHzFoot(card, forecastEntry, pace, projPct, usedPct)}
    </article>`;
}

function _buildSparklineSVG(history) {
    if (!history || history.length < 2) {
        return `<div style="height:120px;display:flex;align-items:center;justify-content:center;font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-dim);border:1px dashed var(--hairline-2);">No history data</div>`;
    }
    const W = 640, H = 120;
    const sorted = [...history].sort((a, b) => new Date(a.timestamp || a.recorded_at) - new Date(b.timestamp || b.recorded_at));
    const vals = sorted.map(p => {
        if (p.pct_used != null) return p.pct_used;
        if (p.avg_used != null && p.avg_limit) return p.avg_used / p.avg_limit * 100;
        if (p.used_value != null && p.limit_value) return p.used_value / p.limit_value * 100;
        return null;
    }).filter(v => v != null);
    if (vals.length < 2) {
        return `<div style="height:120px;display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--text-dim);border:1px dashed var(--hairline-2);">Insufficient data</div>`;
    }
    const n = vals.length;
    const pts = vals.map((v, i) => `${((i / (n - 1)) * W).toFixed(1)},${(H - (v / 100) * H).toFixed(1)}`).join(' ');
    const fill = `0,${H} ${pts} ${W},${H}`;
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:120px;display:block;">
        <defs><linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.28"/>
            <stop offset="100%" stop-color="var(--accent)" stop-opacity="0.02"/>
        </linearGradient></defs>
        <polygon points="${fill}" fill="url(#sparkFill)"/>
        <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
    </svg>`;
}

/**
 * Build content for the per-card detail modal (v2).
 * @param {Object} card           - LimitCard from STATE.data
 * @param {Object|null} forecastEntry - ForecastEntry or null
 * @param {Array}  history24h     - Array from fetchHistoryRaw
 * @returns {string} Inner HTML for #modal-content
 */
export function buildCardModalContent(card, forecastEntry, history24h) {
    const prov = _PROV_MAP[card.provider_id] || { key: 'default', init: (card.provider_id || '??').slice(0, 2).toUpperCase() };
    const pace = _forecastPaceBucket(forecastEntry);

    const pctUsed = card.pct_used != null ? card.pct_used
        : (card.used_value != null && card.limit_value ? card.used_value / card.limit_value * 100 : null);
    const remainingPct = pctUsed != null ? (100 - pctUsed).toFixed(1) : '—';

    const { used: usedFmt, limit: limitFmt, unit } = formatUsageValues(
        card.used_value, card.limit_value, card.unit_type, card.currency
    );

    const paceRow = pace
        ? `<dt>Pace</dt><dd><span class="pace-dot ${pace}" style="display:inline-block;"></span>${pace}</dd>`
        : '';
    const projRow = forecastEntry?.projected_pct != null
        ? `<dt>Projected</dt><dd>${Math.round(forecastEntry.projected_pct)}% used</dd>`
        : '';
    const tierRow = card.tier
        ? `<dt>Tier</dt><dd>${escapeHTML(String(card.tier))}</dd>`
        : '';
    const sidecarRow = card.sidecar_id
        ? `<dt>Sidecar</dt><dd>${escapeHTML(card.sidecar_id)}</dd>`
        : '';

    return `
        <div class="modal-v2-hd">
            <div class="plogo c-${prov.key}" style="width:28px;height:28px;display:grid;place-items:center;box-shadow:inset 0 0 0 1px var(--hairline-2);font-size:${card.icon ? '14px' : '10px'};font-weight:700;flex-shrink:0;">${escapeHTML(card.icon || prov.init)}</div>
            <div class="stack-xs">
                <span class="title">${escapeHTML(card.service_name)}</span>
                ${_windowSubtitle(card)}
                <span style="font-size:10px;color:var(--text-dim);letter-spacing:0.06em;text-transform:uppercase;">${escapeHTML(card.account_label || '')} · ${escapeHTML(card.data_source || '')} · ${escapeHTML(formatRelativeTime(card.updated_at))}</span>
            </div>
            <div style="margin-left:auto;display:flex;align-items:center;gap:4px;">
                <button class="icon-btn" onclick="openProviderInHistory('${escapeHTMLAttr(card.provider_id || '')}')" title="Open in History">↗</button>
                <button class="x" id="close-modal" style="margin-left:0;cursor:pointer;color:var(--text-dim);font-size:20px;background:none;border:none;padding:0 4px;line-height:1;">✕</button>
            </div>
        </div>
        <div class="modal-v2-body">
            <div>
                <h4>Metadata</h4>
                <dl class="kv">
                    <dt>Provider</dt><dd>${escapeHTML(card.provider_id || '—')}</dd>
                    <dt>Source</dt><dd>${escapeHTML(card.data_source || '—')}</dd>
                    ${tierRow}${sidecarRow}
                    <dt>Updated</dt><dd>${escapeHTML(formatRelativeTime(card.updated_at))}</dd>
                </dl>
            </div>
            <div>
                <h4>Current Window</h4>
                <dl class="kv">
                    <dt>Remaining</dt><dd>${remainingPct}%</dd>
                    <dt>Used</dt><dd>${escapeHTML(usedFmt)} / ${escapeHTML(limitFmt)}${unit ? ' ' + unit : ''}</dd>
                    <dt>Window</dt><dd>${escapeHTML(_windowLabel(card.window_type))}</dd>
                    <dt>Reset</dt><dd>${escapeHTML(card.reset_at ? formatResetDisplay(card.reset_at) : (card.reset || '—'))}</dd>
                    ${paceRow}${projRow}
                </dl>
            </div>
            ${card.token_usage ? `
            <div>
                <h4>Token Usage (Session)</h4>
                <dl class="kv">
                    <dt>Input</dt><dd>${card.token_usage.input?.toLocaleString() || '—'}</dd>
                    <dt>Output</dt><dd>${card.token_usage.output?.toLocaleString() || '—'}</dd>
                    ${card.token_usage.cache_read ? `<dt>Cached</dt><dd>${card.token_usage.cache_read.toLocaleString()}</dd>` : ''}
                    ${card.token_usage.reasoning ? `<dt>Reasoning</dt><dd>${card.token_usage.reasoning.toLocaleString()}</dd>` : ''}
                    <dt>Total</dt><dd>${card.token_usage.total?.toLocaleString() || '—'}</dd>
                </dl>
            </div>
            ` : ''}
            ${card.by_model && Object.keys(card.by_model).length > 0 ? `
            <div>
                <h4>By Model</h4>
                <dl class="kv">
                    ${Object.entries(card.by_model).map(([model, data]) =>
                        `<dt>${escapeHTML(model)}</dt><dd>${data.msgs ?? 0} msgs</dd>`
                    ).join('')}
                </dl>
            </div>
            ` : ''}
            <div class="historyline">
                <div style="font-size:9px;font-weight:700;color:var(--text-dim);letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px;">Last 24 hours</div>
                ${_buildSparklineSVG(history24h)}
            </div>
            <details style="margin-top:1rem;font-size:10px;">
                <summary style="cursor:pointer;color:var(--text-dim);letter-spacing:0.08em;text-transform:uppercase;list-style:none;display:flex;align-items:center;gap:6px;"><span style="font-size:9px;border:1px solid var(--hairline-strong);padding:1px 5px;">▶</span> Raw data</summary>
                <pre style="margin-top:8px;padding:12px;background:var(--surface-2);border:1px solid var(--hairline);overflow-x:auto;font-size:10px;color:var(--text-muted);max-height:300px;overflow-y:auto;line-height:1.5;">${escapeHTML(JSON.stringify(card, null, 2))}</pre>
            </details>
        </div>`;
}


// ─────────────────────────────────────────────────────────────────────────
// Fleet HUD: Fleet Commander cards, Wingman Pods, Fuel Dump bar, Status LEDs
// (Spec §5.1 / §5.2 — driven by /api/v1/usage/fleet)
// ─────────────────────────────────────────────────────────────────────────

/**
 * Render a Fleet Commander card for one (provider_id, account_id) group.
 * Wraps the existing horizon-card renderer for the critical gauge and adds:
 *   - LED row showing the health of each secondary limit
 *   - Fuel Dump bar segmented by sidecar contribution
 *   - Collapsible Wingman Pods row driven by the Fuel Dump click
 *
 * @param {object} entry             - One fleet[] entry from /api/v1/usage/fleet
 * @param {Map}    forecastMap       - Map<seriesKey, ForecastEntry> for glide path
 * @returns {string} HTML
 */

// buildFleetCommanderCard moved to ./components/fleet-commander.js — re-exported
// here for backward-compat with `import { buildFleetCommanderCard } from './components.js'`.
export { buildFleetCommanderCard } from './components/fleet-commander.js';
