import { HEALTH_CONFIG, STATE, ERROR_TYPES } from './state.js';
import { pickBucketSeconds } from './charts.js';
import { cardKey, applyOrder } from './layout.js';

/**
 * Escapes HTML special characters to prevent XSS
 * @param {string} str - String to escape
 * @returns {string} Escaped string
 */
function escapeHTML(str) {
    if (!str) return '';
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return str.replace(/[&<>"']/g, function (m) { return map[m]; });
}

/**
 * Escapes a value for safe use inside an HTML attribute (e.g. onclick="setFilter('...')")
 * Escapes single quotes and backslashes only — output is safe to wrap in single quotes.
 * @param {string} str
 * @returns {string}
 */
export function escapeHTMLAttr(str) {
    if (!str) return '';
    return String(str).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

const PROVIDER_ICONS = {
    anthropic: '🟠', gemini: '✨', github: '🐙', chatgpt: '🤖',
    openrouter: '🚀', opencode: '⚡', ollama: '🦙', minimax: '💎',
    kimi_api: '🌊', kimi_coding: '💻', kimi_k2: '🌙', zai: '🌐',
    antigravity: '🪐',
};

/**
 * Build HTML for a provider section grouping multiple cards
 * @param {string} providerId - Provider identifier or '__other__'
 * @param {Array} items - Limit card data items for this provider
 * @returns {string} HTML string for the provider section
 */
export function buildProviderSection(providerId, items) {
    const title = providerId === '__other__' ? 'Other' : providerId;
    const icon = PROVIDER_ICONS[providerId] || '🔧';
    const cards = items.map(buildCard).filter(Boolean).join('');
    if (!cards) return '';
    return `<div class="provider-section mb-8" data-provider-id="${escapeHTMLAttr(providerId)}">
        <div class="flex items-center gap-2 mb-3 pb-2 border-b border-zinc-800/40">
            <span>${icon}</span>
            <h3 class="text-xs font-bold text-zinc-400 uppercase tracking-widest">${escapeHTML(title)}</h3>
            <span class="text-[10px] text-zinc-600 mono">${items.length}</span>
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-5">${cards}</div>
    </div>`;
}

/**
 * Returns a styled tier badge based on the tier name
 * @param {string} tier - Tier name (Free, Pro, Team, etc.)
 * @returns {string} HTML for the badge, or empty string if no tier
 */
function getTierBadge(tier) {
    // Don't show badge if tier is null, undefined, or empty
    if (tier === null || tier === undefined || tier === '') return '';
    const t = tier.toLowerCase();
    let classes = 'bg-zinc-800/50 text-zinc-500 border-zinc-700/50'; // Default/Free
    let style = '';

    if (t.includes('max')) {
        // Use inline style to bypass Tailwind JIT stripping
        style = 'style="color: #c084fc; background-color: rgba(192, 132, 252, 0.1); border-color: rgba(192, 132, 252, 0.2)"';
        classes = ''; 
    } else if (t.includes('pro') || t.includes('premium') || t.includes('plus')) {
        classes = 'bg-amber-500/10 text-amber-400 border-amber-500/20';
    } else if (t.includes('team') || t.includes('enterprise') || t.includes('organization')) {
        classes = 'bg-violet-500/10 text-violet-400 border-violet-500/20';
    } else if (t.includes('free')) {
        classes = 'bg-zinc-800/50 text-zinc-500 border-zinc-700/50';
    }

    return `<span class="text-sm font-bold px-1.5 py-0.5 rounded border leading-none uppercase tracking-widest ${classes}" ${style}>${escapeHTML(tier)}</span>`;
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

/**
 * Format a number with appropriate abbreviations (K, M, B)
 * @param {number} num - Number to format
 * @returns {string} Formatted number
 */
function formatNumber(num) {
    if (num === null || num === undefined || isNaN(num)) return '—';
    const absNum = Math.abs(num);
    if (absNum >= 1e9) return `${(num / 1e9).toFixed(1)}B`;
    if (absNum >= 1e6) return `${(num / 1e6).toFixed(1)}M`;
    if (absNum >= 1e3) return `${(num / 1e3).toFixed(1)}K`;
    if (Number.isInteger(num)) return num.toString();
    return num.toFixed(1);
}

/**
 * Format currency amount with appropriate symbol
 * @param {number} amount - Amount to format
 * @param {string} currencyCode - Currency code (USD, EUR, etc.)
 * @returns {string} Formatted currency
 */
function formatCurrency(amount, currencyCode) {
    if (amount === null || amount === undefined || isNaN(amount)) return '—';
    const symbols = { USD: '$', EUR: '€', GBP: '£', CNY: '¥', JPY: '¥' };
    const symbol = symbols[currencyCode] || '$';
    return `${symbol}${amount.toFixed(2)}`;
}

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
function formatHumanDelta(targetDate) {
    const now = new Date();
    const diffMs = targetDate - now;
    const seconds = Math.floor(diffMs / 1000);

    if (seconds < 0) return 'Just now';
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) {
        const hours = Math.floor(seconds / 3600);
        const mins = Math.floor((seconds % 3600) / 60);
        return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
    }
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
}

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

        // If > 24h away, show relative time (like "12d 4h")
        if (diffHours >= 24) {
            return formatHumanDelta(date);
        }

        // If <= 24h, show local time (e.g., "Resets at 10:43 PM")
        const timeStr = date.toLocaleTimeString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            hour12: undefined,
            timeZoneName: 'short'
        });
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
 * Build an HTML card element for a limit
 * @param {LimitCard} item - The limit card data
 * @returns {string} HTML string representing the card
 */
export function buildCard(item) {
    const isUnlimited = item.is_unlimited || item.health === 'unlimited';
    let h = HEALTH_CONFIG[item.health] || HEALTH_CONFIG.unknown;
    let errorIcon = '';

    // Categorize error if present
    if (item.health === 'critical' && item.error_type && ERROR_TYPES[item.error_type]) {
        const errConfig = ERROR_TYPES[item.error_type];
        h = { ...h, badge: errConfig.color, label: errConfig.label };
        errorIcon = errConfig.icon + ' ';
    }

    // Calculate percentage
    let usedPct = calculateUsedPct(item);
    const hasPercentage = usedPct !== null;

    // For display: default shows % used, toggle shows % remaining
    let displayPct = usedPct;
    let displayLabel = 'used';
    if (STATE.remaining && hasPercentage) {
        displayPct = 100 - usedPct;
        displayLabel = 'rem.';
    }

    // For unlimited plans, show special display
    if (isUnlimited) {
        displayPct = null;
    }

    // Progress bar width
    let barWidth = hasPercentage ? usedPct : 0;
    if (STATE.remaining && hasPercentage) {
        barWidth = 100 - usedPct;
    }

    const isPlaceholder = item.health === 'unknown';

    // Handle Compact Mode
    if (STATE.compact) {
        const mainDisplay = buildMainDisplay(isUnlimited, hasPercentage, displayPct, item.remaining, isPlaceholder, 'text-2xl leading-none');
        const progressBar = buildProgressBar(isUnlimited, barWidth, h.bar, 'progress-track mt-auto overflow-hidden rounded-full bg-zinc-800/50');

        return `
            <div class="glass-panel ${h.card} rounded-xl p-3 relative flex flex-col gap-2 cursor-pointer select-none active:scale-[0.98] transition-all duration-200" data-service="${escapeHTML(item.service_name)}" data-card-key="${escapeHTMLAttr(cardKey(item))}">
                <span class="drag-handle" aria-hidden="true" onclick="event.stopPropagation()">
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                        <circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/>
                        <circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/>
                        <circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/>
                    </svg>
                </span>
                <div class="flex items-center justify-between gap-2">
                    <div class="flex items-center gap-1.5 min-w-0">
                        <span class="text-base leading-none">${escapeHTML(item.icon)}</span>
                        <span class="text-[10px] font-bold text-zinc-400 uppercase tracking-tight truncate">${escapeHTML(item.service_name)}</span>
                    </div>
                    <div class="dot ${h.dot} shrink-0"></div>
                </div>

                <div class="flex items-end justify-between gap-1 mt-1">
                    <div class="flex flex-col min-w-0">
                        <div class="flex items-baseline gap-1">
                            ${mainDisplay}
                            ${!isUnlimited && hasPercentage ? `<span class="text-[8px] font-bold text-zinc-500 uppercase">${displayLabel}</span>` : ''}
                        </div>
                        <span class="text-[8px] mono truncate opacity-50 uppercase">${item.data_source || ''}</span>
                    </div>
                    <span class="text-[9px] text-zinc-500 mono leading-none mb-0.5 truncate max-w-[160px]" title="${escapeHTML(item.reset)}">${escapeHTML(item.reset)}</span>
                </div>

                ${progressBar}
            </div>
        `;
    }

    // Standard Layout (Below)
    displayLabel = STATE.remaining ? 'remaining' : 'used';

    // Build subtitle with raw values and data source
    let subtitle = '';
    const sourceLabel = formatDataSource(item.data_source);
    if (isUnlimited) {
        subtitle = `<span class="text-xs text-zinc-500 truncate">No usage limit${sourceLabel}</span>`;
    } else if (hasPercentage && item.used_value !== null && item.limit_value !== null) {
        const displayValue = STATE.remaining ? Math.max(0, item.limit_value - item.used_value) : item.used_value;
        const formatted = formatUsageValues(
            displayValue,
            item.limit_value,
            item.unit_type || 'generic',
            item.currency
        );
        subtitle = `<span class="text-xs text-zinc-500 truncate">${escapeHTML(formatted.used)} of ${escapeHTML(formatted.limit)} ${escapeHTML(formatted.unit)} ${displayLabel}${sourceLabel}</span>`;
    } else if (item.detail) {
        // Fallback to detail field
        const escapedDetail = escapeHTML(item.detail);
        subtitle = `<span class="text-xs text-zinc-600 mono truncate block" title="${escapedDetail}">${errorIcon}${escapedDetail}${sourceLabel}</span>`;
    }

    const progressBar = hasPercentage || isUnlimited
        ? buildProgressBar(isUnlimited, barWidth, h.bar, 'progress-track mt-3')
        : '';

    const unitTrailing = `\n                <span class="text-sm font-medium text-zinc-500">${escapeHTML(item.unit)}</span>`;
    const mainDisplay = buildMainDisplay(isUnlimited, hasPercentage, displayPct, item.remaining, isPlaceholder, 'text-4xl', unitTrailing);

    // For unlimited plans, add unit label next to infinity
    const unitLabel = isUnlimited ? `<span class="text-sm font-medium text-zinc-500 ml-2">${escapeHTML(item.unit || 'Unlimited')}</span>` : '';

    // Pace icon with tooltip (styled like resets tooltip)
    const paceIcon = item.pace ? `
        <div class="tooltip-container">
            <span class="pace-icon cursor-help">${getPaceIcon(item.pace)}</span>
            <div class="tooltip">Pace: ${escapeHTML(item.pace)}</div>
        </div>
    ` : '';

    // Build reset element with tooltip (use reset_at for consistent timezone handling)
    const resetDisplay = formatResetDisplay(item.reset_at);
    const resetTooltip = formatResetTooltip(item.reset_at);
    const resetElement = resetTooltip ? `
        <div class="tooltip-container">
            <span class="text-xs font-semibold text-zinc-400 bg-zinc-800/60 px-2 py-1 rounded-md mono cursor-help">
                ${escapeHTML(resetDisplay)}
            </span>
            <div class="tooltip">${resetTooltip}</div>
        </div>
    ` : `<span class="text-xs font-semibold text-zinc-400 bg-zinc-800/60 px-2 py-1 rounded-md mono">${escapeHTML(resetDisplay)}</span>`;

    const sourceBadge = item.sidecar_id
        ? `<div class="source-badge" title="${escapeHTML(item.sidecar_id)}">${escapeHTML(item.sidecar_id[0].toUpperCase())}</div>`
        : '';

    return `
        <div class="glass-panel ${h.card} rounded-2xl overflow-hidden relative card-layout cursor-pointer select-none active:scale-[0.98] transition-all duration-200" data-service="${escapeHTML(item.service_name)}" data-card-key="${escapeHTMLAttr(cardKey(item))}">
            <span class="drag-handle" aria-hidden="true" onclick="event.stopPropagation()">
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                    <circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/>
                    <circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/>
                    <circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/>
                </svg>
            </span>
            ${sourceBadge}
            <!-- Top zone (grid row: 1fr) -->
            <div class="p-5 flex flex-col gap-3">
                <!-- Header row -->
                <div class="flex items-start justify-between gap-2">
                    <div class="flex items-center gap-2 min-w-0">
                        <span class="text-xl leading-none">${escapeHTML(item.icon)}</span>
                        <div class="flex flex-col">
                            <span class="text-[10px] font-semibold text-zinc-400 uppercase tracking-wide truncate">${escapeHTML(item.service_name)}</span>
                            ${getTierBadge(item.tier)}
                        </div>
                    </div>
                    <div class="flex items-center gap-1.5 shrink-0">
                        <div class="dot ${h.dot}"></div>
                        <span class="text-sm font-bold ${h.badge} mono">${h.label}</span>
                    </div>
                </div>

                ${progressBar}

                <!-- Main value with pace icon -->
                <div class="mt-1">
                    <div class="flex items-center justify-between">
                        <div class="flex items-baseline gap-1.5 flex-wrap">
                            ${mainDisplay}${unitLabel}
                        </div>
                        ${paceIcon}
                    </div>
                    <div class="mt-1">
                        ${subtitle}
                    </div>
                </div>
            </div>

            <!-- Reset footer (bottom zone with distinct bg) -->
            <div class="border-t border-zinc-800/50 bg-black/20 px-5 py-3.5 flex items-center justify-between">
                <span class="text-xs text-zinc-600 mono font-medium">RESETS</span>
                ${resetElement}
            </div>
        </div>
    `;
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
    const resetTime = item.reset_at ? new Date(item.reset_at).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : 'Never';
    const updatedTime = formatRelativeTime(item.updated_at);

    const isAuthFailed = item.error_type === 'auth_failed';
    const retryButton = isAuthFailed ? `
        <button 
            onclick="event.stopPropagation(); window.handleResetProvider('${escapeHTML(item.provider_id)}', '${escapeHTML(item.account_id)}')"
            class="mt-4 w-full py-3 bg-amber-500 hover:bg-amber-400 text-black text-xs font-bold rounded-xl transition-all uppercase tracking-widest shadow-lg shadow-amber-900/20"
        >Retry Authentication</button>
    ` : '';

    const linkButton = item.usage_url ? `
        <a href="${escapeHTML(item.usage_url)}" target="_blank" rel="noopener noreferrer" class="w-10 h-10 flex items-center justify-center rounded-full hover:bg-zinc-800 transition-colors text-zinc-400" title="Open usage page">
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
        </a>
    ` : '';

    return `
        <div class="modal-header border-b border-zinc-800/80">
            <div class="flex items-center justify-between mb-4">
                <div class="flex items-center gap-3">
                    <span class="text-3xl">${escapeHTML(item.icon)}</span>
                    <div>
                        <h2 class="text-xl font-black text-zinc-50 tracking-tight">${escapeHTML(item.service_name)}</h2>
                        <div class="flex items-center gap-2 mt-0.5">
                            <span class="text-sm font-bold px-1.5 py-0.5 rounded border leading-none uppercase tracking-widest mono ${h.badge} ${healthBoxClasses[item.health] || healthBoxClasses.unknown}">${h.label}</span>
                            ${getTierBadge(item.tier)}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-1">
                    ${linkButton}
                    <button id="refresh-provider-btn" title="Refresh now" class="w-10 h-10 flex items-center justify-center rounded-full hover:bg-zinc-800 transition-colors text-zinc-400 hover:text-zinc-100">
                        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
                    </button>
                    <button id="close-modal" class="w-10 h-10 flex items-center justify-center rounded-full hover:bg-zinc-800 transition-colors text-zinc-400">
                        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                    </button>
                </div>
            </div>
            
            <div class="mt-6 flex flex-col gap-2">
                <div class="flex items-baseline justify-between">
                    <span class="text-6xl font-black tracking-tighter text-zinc-50">
                        ${isUnlimited ? '∞' : (usedPct ? usedPct.toFixed(1) + '%' : escapeHTML(item.remaining))}
                    </span>
                    <span class="text-sm font-bold text-zinc-500 mono uppercase">${isUnlimited ? 'Unlimited Capacity' : 'Used Capacity'}</span>
                </div>
                
                <div class="progress-track h-2 w-full bg-zinc-800/50 rounded-full mt-2">
                    <div class="progress-fill h-full rounded-full" 
                         style="width: ${isUnlimited ? 100 : (usedPct || 0)}%; background: ${isUnlimited ? 'linear-gradient(90deg, #ff0080, #ff8c00, #40e0d0)' : h.bar};">
                    </div>
                </div>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mt-2">
            <div class="modal-detail-item flex flex-col gap-1">
                <span class="text-xl font-bold text-zinc-500 uppercase tracking-widest">Usage Value</span>
                <span class="text-2xl font-semibold text-zinc-200 mono">${escapeHTML(formatted.used)}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span class="text-xl font-bold text-zinc-500 uppercase tracking-widest">Service Limit</span>
                <span class="text-2xl font-semibold text-zinc-200 mono">${isUnlimited ? '∞' : escapeHTML(formatted.limit)} ${escapeHTML(formatted.unit)}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span class="text-xl font-bold text-zinc-500 uppercase tracking-widest">Resets At</span>
                <span class="text-2xl font-semibold text-zinc-200 mono">${resetTime}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span class="text-xl font-bold text-zinc-500 uppercase tracking-widest">Data Source</span>
                <span class="text-2xl font-bold ${sourceColor} mono">● ${sourceLabel}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span class="text-xl font-bold text-zinc-500 uppercase tracking-widest">Account</span>
                <span class="text-2xl font-semibold text-zinc-200 mono truncate" title="${escapeHTML(item.account_label || 'Default')}">${escapeHTML(item.account_label || 'Default')}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span class="text-xl font-bold text-zinc-500 uppercase tracking-widest">Last Updated</span>
                <span class="text-2xl font-semibold text-zinc-200 mono">${updatedTime}</span>
            </div>
        </div>

        ${item.detail ? `
        <div class="mt-6 p-4 rounded-2xl bg-black/40 border border-zinc-800/60">
            <span class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest block mb-3">Diagnostic Summary</span>
            <div class="space-y-3">
                <div class="flex flex-col gap-1">
                    <span class="text-xs font-bold text-zinc-500 uppercase tracking-tight">Backend Source</span>
                    <p class="text-xs text-zinc-300 mono leading-relaxed">${escapeHTML(item.detail)}</p>
                </div>
                <div class="grid grid-cols-2 gap-3 pt-2 border-t border-zinc-800/40">
                    <div class="flex flex-col gap-0.5">
                        <span class="text-xs font-bold text-zinc-600 uppercase">Provider Key</span>
                        <span class="text-xs text-zinc-400 mono">${escapeHTML(item.provider_id || 'unknown')}</span>
                    </div>
                    <div class="flex flex-col gap-0.5">
                        <span class="text-xs font-bold text-zinc-600 uppercase">Snapshot ID</span>
                        <span class="text-xs text-zinc-400 mono">${escapeHTML(item.account_id || 'default')}</span>
                    </div>
                </div>
                <div class="pt-2">
                    <p class="text-[9px] text-zinc-600 italic leading-snug">This data is cached in the local SQLite database and synchronized with the in-memory registry for instant delivery.</p>
                </div>
            </div>
            ${item.detail.includes('App-Bound Encryption') ? `
                <div class="mt-3 p-2 bg-blue-500/10 border border-blue-500/20 rounded-lg">
                    <p class="text-[10px] text-blue-300">💡 <strong>Tip:</strong> Chrome 127+ uses App-Bound Encryption. Try using <strong>Safari</strong> or set credentials via environment variables.</p>
                </div>
            ` : ''}
        </div>
        ` : ''}

        ${item.pace ? `
        <div class="mt-4 flex items-center justify-between px-1">
            <span class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest">Consumption Rate</span>
            <span class="text-xs font-bold text-zinc-400 mono">${escapeHTML(item.pace)}</span>
        </div>
        ` : ''}

        ${retryButton}

        ${(item.service_name.toLowerCase().includes('github') || item.service_name.toLowerCase().includes('copilot')) ? `
        <div class="mt-6 p-4 rounded-2xl bg-zinc-900/40 border border-zinc-800/60 flex flex-col gap-3">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                    <span class="text-lg">🐙</span>
                    <span class="text-sm font-bold text-zinc-200 uppercase tracking-tight">GitHub Authentication</span>
                </div>
                ${STATE.githubAuth.authenticated ? `
                    <span class="text-[10px] font-bold text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20 uppercase tracking-tighter">Connected</span>
                ` : `
                    <span class="text-[10px] font-bold text-zinc-500 bg-zinc-800/50 px-2 py-0.5 rounded border border-zinc-700/50 uppercase tracking-tighter">Disconnected</span>
                `}
            </div>
            
            ${STATE.githubAuth.authenticated ? `
                <div class="flex items-center justify-between gap-4">
                    <div class="flex flex-col">
                        <span class="text-xs font-bold text-zinc-500 uppercase tracking-widest">Account</span>
                        <span class="text-sm font-semibold text-zinc-300 mono">${escapeHTML(STATE.githubAuth.account || STATE.githubAuth.name || 'Account')}${STATE.githubAuth.email ? ` <span class="text-zinc-500 font-normal">(${escapeHTML(STATE.githubAuth.email)})</span>` : ''}</span>
                    </div>
                    <button 
                        onclick="event.stopPropagation(); window.handleGitHubLogout()"
                        class="px-4 py-2 bg-zinc-800 hover:bg-red-500/10 hover:text-red-400 text-zinc-400 text-[10px] font-bold rounded-lg border border-zinc-700/50 transition-all uppercase tracking-widest"
                    >Logout</button>
                </div>
            ` : `
                <div class="flex flex-col gap-3">
                    <p class="text-xs text-zinc-500 leading-relaxed">Connect your GitHub account to fetch live Copilot usage limits directly from the GitHub API.</p>
                    <button 
                        onclick="event.stopPropagation(); window.startGitHubLogin()"
                        class="w-full py-3 bg-blue-600 hover:bg-blue-500 text-white text-xs font-bold rounded-xl transition-all uppercase tracking-widest shadow-lg shadow-blue-900/20"
                    >Connect GitHub</button>
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
                <div class="w-16 h-16 bg-red-500/10 text-red-500 rounded-full flex items-center justify-center mx-auto mb-4">
                    <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>
                </div>
                <h2 class="text-xl font-black text-zinc-50 mb-2">Connection Failed</h2>
                <p class="text-zinc-400 text-sm mb-6">${escapeHTML(error)}</p>
                <button id="close-modal" class="w-full py-3 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 font-bold rounded-xl transition-all">CLOSE</button>
            </div>
        `;
    }

    if (!data) {
        return `
            <div class="p-12 text-center">
                <div class="inline-block w-8 h-8 border-4 border-blue-500/30 border-t-blue-500 rounded-full animate-spin mb-4"></div>
                <p class="text-zinc-500 font-bold tracking-widest text-xs uppercase">Initializing GitHub Login...</p>
            </div>
        `;
    }

    return `
        <div class="p-2">
            <div class="flex items-center justify-between mb-8">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 bg-zinc-50 text-zinc-950 rounded-full flex items-center justify-center font-bold">🐙</div>
                    <h2 class="text-xl font-black text-zinc-50 tracking-tight">Connect GitHub</h2>
                </div>
                <button id="close-modal" class="w-10 h-10 flex items-center justify-center rounded-full hover:bg-zinc-800 transition-colors text-zinc-400">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                </button>
            </div>

            <div class="space-y-6">
                <div class="text-center">
                    <p class="text-zinc-400 text-sm mb-1">Enter this code on GitHub to authorize Runway:</p>
                    <div class="text-4xl font-black tracking-[0.2em] text-blue-400 mono py-4 bg-blue-500/5 rounded-2xl border border-blue-500/20 my-4 select-all">
                        ${escapeHTML(data.user_code)}
                    </div>
                </div>

                <div class="bg-zinc-900/50 rounded-2xl p-5 border border-zinc-800/50">
                    <ol class="space-y-3 text-sm text-zinc-300">
                        <li class="flex gap-3">
                            <span class="flex-shrink-0 w-5 h-5 bg-zinc-800 text-zinc-400 rounded-full flex items-center justify-center text-[10px] font-bold">1</span>
                            <span>Open <a href="${escapeHTML(data.verification_uri)}" target="_blank" class="text-blue-400 hover:underline font-bold">${escapeHTML(data.verification_uri)}</a></span>
                        </li>
                        <li class="flex gap-3">
                            <span class="flex-shrink-0 w-5 h-5 bg-zinc-800 text-zinc-400 rounded-full flex items-center justify-center text-[10px] font-bold">2</span>
                            <span>Enter the 8-character code shown above.</span>
                        </li>
                        <li class="flex gap-3">
                            <span class="flex-shrink-0 w-5 h-5 bg-zinc-800 text-zinc-400 rounded-full flex items-center justify-center text-[10px] font-bold">3</span>
                            <span>Once authorized, this window will close automatically.</span>
                        </li>
                    </ol>
                </div>

                <div class="flex items-center justify-center gap-3 py-2">
                    <div class="flex gap-1">
                        <div class="w-1.5 h-1.5 bg-blue-500 rounded-full animate-bounce [animation-delay:-0.3s]"></div>
                        <div class="w-1.5 h-1.5 bg-blue-500 rounded-full animate-bounce [animation-delay:-0.15s]"></div>
                        <div class="w-1.5 h-1.5 bg-blue-500 rounded-full animate-bounce"></div>
                    </div>
                    <span class="text-xs font-bold text-zinc-500 uppercase tracking-widest">Waiting for authorization...</span>
                </div>

                <button id="cancel-github-login" class="w-full py-3 bg-zinc-800 hover:bg-zinc-700 text-zinc-400 font-bold rounded-xl transition-all text-xs uppercase tracking-widest">CANCEL</button>
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
        return `<div class="mt-8 pt-6 border-t border-zinc-800/80">
            <h3 class="text-sm font-bold text-zinc-300 mb-3 flex items-center gap-2"><span>🔑</span> Token Health</h3>
            <p class="text-xs text-zinc-600 italic">No active credentials in cache.</p>
        </div>`;
    }

    const STATUS_STYLES = {
        valid:    { badge: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20', label: 'READY' },
        expiring: { badge: 'text-amber-400 bg-amber-500/10 border-amber-500/20', label: 'EXPIRING' },
        expired:  { badge: 'text-red-400 bg-red-500/10 border-red-500/20', label: 'EXPIRED' },
        unknown:  { badge: 'text-zinc-500 bg-zinc-800/50 border-zinc-700/50', label: 'UNKNOWN' },
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
            ? new Date(t.expires_at).toLocaleString()
            : t.ttl_remaining_seconds > 0
            ? `cache TTL ${t.ttl_remaining_seconds}s`
            : '';

        const refreshBtn = (t.can_refresh && t.status !== 'valid') ? `
            <button onclick="window.refreshToken('${escapeHTMLAttr(t.provider)}', '${escapeHTMLAttr(t.account_id)}')"
                    class="text-[9px] font-bold px-2 py-0.5 rounded border border-violet-500/40 text-violet-400 hover:bg-violet-500/10 transition-all uppercase tracking-wider">
                REFRESH
            </button>` : '';
        
        const purgeBtn = `
            <button onclick="window.deleteToken('${escapeHTMLAttr(t.provider)}', '${escapeHTMLAttr(t.account_id)}')"
                    class="w-7 h-7 flex items-center justify-center rounded-lg text-zinc-600 hover:text-red-400 hover:bg-red-500/10 transition-all ml-1"
                    title="Purge from cache">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4h6v2"></path></svg>
            </button>`;

        const sourceBadge = t.source === 'config'
            ? `<span class="text-[9px] font-mono px-1.5 py-0.5 rounded bg-amber-500/10 border border-amber-500/20 text-amber-400" title="Configured in Settings → Providers">⚙ settings</span>`
            : t.source
            ? `<span class="text-[9px] font-mono px-1.5 py-0.5 rounded bg-violet-500/10 border border-violet-500/20 text-violet-400" title="Delivered by sidecar">⬡ ${escapeHTML(t.source)}</span>`
            : `<span class="text-[9px] font-mono px-1.5 py-0.5 rounded bg-zinc-800/60 border border-zinc-700/40 text-zinc-500" title="Collected locally">local</span>`;

        return `
        <div class="flex items-center justify-between gap-2 py-3 border-b border-zinc-800/40 last:border-0">
            <div class="flex flex-col min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                    <span class="text-xs font-bold text-zinc-200 mono">${escapeHTML(t.provider)}</span>
                    <span class="text-[10px] text-zinc-500">${escapeHTML(label)}</span>
                    ${sourceBadge}
                </div>
                <div class="flex flex-wrap gap-1 mt-1">${typesHTML}</div>
                ${expiryStr ? `<span class="text-[10px] text-zinc-600 mono mt-0.5">${escapeHTML(expiryStr)}</span>` : ''}
            </div>
            <div class="flex items-center gap-1 shrink-0">
                ${refreshBtn}
                ${purgeBtn}
                <span class="text-[9px] font-bold px-2 py-0.5 rounded border ${s.badge} uppercase tracking-wider">${s.label}</span>
            </div>
        </div>`;
    }).join('');

    return `<div class="mt-8 pt-6 border-t border-zinc-800/80">
        <h3 class="text-sm font-bold text-zinc-300 mb-3 flex items-center gap-2"><span>🔑</span> Token Health</h3>
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
        let dotClass, dotTitle;
        if (ageMs < 30 * 60 * 1000) {
            dotClass = 'dot-good'; dotTitle = 'Active (seen < 30m ago)';
        } else if (ageMs < 2 * 60 * 60 * 1000) {
            dotClass = 'dot-warning'; dotTitle = 'Idle (seen < 2h ago)';
        } else {
            dotClass = 'dot-unknown'; dotTitle = 'Stale';
        }

        const displayName = s.custom_name || s.hostname || s.sidecar_id;
        const tags = (s.tags || []).map(t => `<span class="tag-pill">${escapeHTML(t)}</span>`).join('');
        const lastSeenStr = lastSeen ? lastSeen.toLocaleString() : '—';
        const staleBanner = s.stale
            ? `<div class="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-[10px]">
                   <span class="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0"></span>
                   No check-in for over ${s.stale_threshold_minutes ?? 60} minutes
               </div>`
            : '';

        return `
        <div class="glass-panel rounded-2xl p-5 flex flex-col gap-3" data-sidecar="${escapeHTML(s.sidecar_id)}">
            <div class="flex items-start justify-between gap-2">
                <div class="flex items-center gap-2 min-w-0">
                    <div class="dot ${dotClass}" title="${dotTitle}"></div>
                    <div class="flex flex-col min-w-0">
                        <span class="fleet-name text-sm font-bold text-zinc-100 truncate cursor-pointer hover:text-violet-300 transition-colors"
                              onclick="window.editSidecarName('${escapeHTMLAttr(s.sidecar_id)}')"
                              title="Click to rename">${escapeHTML(displayName)}</span>
                        <span class="text-[10px] text-zinc-600 mono truncate">${escapeHTML(s.sidecar_id)}</span>
                    </div>
                </div>
                <div class="flex items-center gap-1 shrink-0">
                    <button onclick="window.triggerSidecarCollect('${escapeHTMLAttr(s.sidecar_id)}')"
                            class="w-7 h-7 flex items-center justify-center rounded-lg text-zinc-600 hover:text-green-400 hover:bg-green-500/10 transition-all"
                            title="Run Now — trigger immediate collection">
                        <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                    </button>
                    <button onclick="window.deleteSidecar('${escapeHTMLAttr(s.sidecar_id)}')"
                            class="w-7 h-7 flex items-center justify-center rounded-lg text-zinc-600 hover:text-red-400 hover:bg-red-500/10 transition-all"
                            title="Remove sidecar">
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4h6v2"></path></svg>
                    </button>
                </div>
            </div>
            ${staleBanner}
            <div class="flex flex-wrap gap-1 items-center">
                ${tags}
                <button onclick="window.addSidecarTag('${escapeHTMLAttr(s.sidecar_id)}')"
                        class="text-[9px] font-bold text-zinc-600 hover:text-violet-400 px-1.5 py-0.5 rounded border border-dashed border-zinc-700 hover:border-violet-500/50 transition-all">+ TAG</button>
            </div>
            <div class="pt-2 border-t border-zinc-800/60 grid grid-cols-2 gap-2 text-[10px] text-zinc-500 mono">
                <div><span class="text-zinc-600">LAST SEEN</span><br/>${escapeHTML(lastSeenStr)}</div>
                <div><span class="text-zinc-600">INGESTS</span><br/>${s.ingest_count ?? 0}</div>
                <div><span class="text-zinc-600">IP</span><br/>${escapeHTML(s.last_ip || '—')}</div>
                <div><span class="text-zinc-600">ERRORS</span><br/>${s.error_count ?? 0}</div>
                <div><span class="text-zinc-600">VERSION</span><br/>${escapeHTML(s.sidecar_version || '—')}</div>
                <div><span class="text-zinc-600">OS</span><br/>${escapeHTML(s.os_platform || '—')}</div>
            </div>
            ${(s.recent_logs && s.recent_logs.length > 0) ? `
            <details class="mt-0.5">
                <summary class="text-[9px] text-zinc-600 cursor-pointer hover:text-zinc-400 transition-colors select-none list-none flex items-center gap-1">
                    <svg class="details-arrow" xmlns="http://www.w3.org/2000/svg" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
                    Recent logs (${s.recent_logs.length} lines)
                </summary>
                <pre class="mt-1.5 text-[9px] text-zinc-500 bg-zinc-900/60 rounded-lg p-2 overflow-x-hidden overflow-y-auto whitespace-pre-wrap break-all max-h-32 leading-relaxed mono">${s.recent_logs.map(l => escapeHTML(l)).join('\n')}</pre>
            </details>` : ''}
        </div>`;
    }).join('');

    return `<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">${rows}</div>`;
}

/**
 * Build the top health overview bar (4 stat tiles + proportional bar).
 * @param {Array} data - Full array of LimitCard items from STATE.data
 * @returns {string} HTML string
 */
export function buildHealthBar(data) {
    const counts = { critical: 0, warning: 0, good: 0, unlimited: 0 };
    for (const item of data) {
        if (counts[item.health] !== undefined) counts[item.health]++;
    }
    if (data.length === 0) return '';

    const tiles = [
        { key: 'critical',  label: 'Critical',  textColor: 'text-red-400',    border: 'border-red-900/50',    bg: 'bg-red-950/20'    },
        { key: 'warning',   label: 'Warning',   textColor: 'text-amber-400',  border: 'border-amber-900/50', bg: 'bg-amber-950/20'  },
        { key: 'good',      label: 'Good',      textColor: 'text-green-400',  border: 'border-green-900/50', bg: 'bg-green-950/20'  },
        { key: 'unlimited', label: 'Unlimited', textColor: 'text-violet-400', border: 'border-violet-900/50',bg: 'bg-violet-950/20' },
    ];
    const BAR_HEX = { critical: '#ef4444', warning: '#eab308', good: '#22c55e', unlimited: '#8b5cf6' };

    const tilesHTML = tiles.map(t => `
        <div class="glass-panel ${t.border} ${t.bg} rounded-xl p-3 text-center${counts[t.key] === 0 ? ' opacity-30' : ''}">
            <div class="text-3xl font-black ${t.textColor} leading-none">${counts[t.key]}</div>
            <div class="text-[9px] text-zinc-500 uppercase tracking-widest mt-1.5">${t.label}</div>
        </div>`).join('');

    const barSegments = tiles
        .filter(t => counts[t.key] > 0)
        .map(t => `<div style="flex:${counts[t.key]};background:${BAR_HEX[t.key]};"></div>`)
        .join('');

    return `<div class="mb-6">
        <div class="grid grid-cols-4 gap-3 mb-2">${tilesHTML}</div>
        <div class="h-1.5 rounded-full overflow-hidden flex gap-0.5">${barSegments}</div>
    </div>`;
}

/** Return Tailwind text color class for a tier name (used in breakdown rows) */
function getTierTextClass(tier) {
    if (!tier) return 'text-zinc-500';
    const t = tier.toLowerCase();
    if (t.includes('max')) return 'text-purple-400';
    if (t.includes('pro') || t.includes('premium') || t.includes('plus')) return 'text-amber-400';
    if (t.includes('team') || t.includes('enterprise')) return 'text-violet-400';
    return 'text-zinc-500';
}

/** Health severity for sorting (higher = worse) */
const HEALTH_SEVERITY = { critical: 4, warning: 3, good: 2, unknown: 1, unlimited: 0 };

/**
 * Build a two-zone aggregate provider card for the dashboard grid.
 * Top zone: worst metric, tier, account. Bottom zone: per-service breakdown.
 * @param {string} providerId
 * @param {Array} items - LimitCard items for this provider
 * @returns {string} HTML string
 */
export function buildProviderSummaryCard(providerId, items) {
    if (!items || items.length === 0) return '';

    const icon = PROVIDER_ICONS[providerId] || '🔧';

    // Health-sorted copy — drives the "worst" metric display only.
    const healthSorted = [...items].sort((a, b) => {
        const severityDiff = (HEALTH_SEVERITY[b.health] || 0) - (HEALTH_SEVERITY[a.health] || 0);
        if (severityDiff !== 0) return severityDiff;
        const aRatio = (a.limit_value > 0) ? a.used_value / a.limit_value : 0;
        const bRatio = (b.limit_value > 0) ? b.used_value / b.limit_value : 0;
        return bRatio - aRatio;
    });
    const worst = healthSorted[0];

    // Breakdown rows follow the user's saved order; new/unpinned items keep
    // the health-first order as their default.
    const ordered = applyOrder(
        healthSorted,
        cardKey,
        STATE.layout?.card_orders?.[providerId] ?? []
    );
    const h = HEALTH_CONFIG[worst.health] || HEALTH_CONFIG.unknown;

    // Worst metric display
    let worstPct = null;
    if (!worst.is_unlimited && worst.used_value != null && worst.limit_value > 0) {
        worstPct = (worst.used_value / worst.limit_value) * 100;
    }
    const worstDisplay = worst.is_unlimited
        ? '∞'
        : worstPct != null
        ? `${worstPct.toFixed(1)}%`
        : escapeHTML(String(worst.remaining ?? '—'));

    const worstColor = worst.is_unlimited ? 'text-violet-400' : h.badge || 'text-zinc-50';

    // Tier badge (first item's tier — same account = same tier)
    const tier = worst.tier;
    const tierBadgeHTML = tier ? getTierBadge(tier) : '';

    // Account and source labels
    const accounts = [...new Set(items.map(i => i.account_label).filter(Boolean))];
    const sources = [...new Set(items.map(i => i.input_source).filter(s => s && s !== 'unknown'))];
    
    let sourceBadge = '';
    if (sources.length > 0) {
        const s = sources[0];
        const label = s === 'sidecar' ? 'Sidecar' : s === 'config' ? 'Config' : s === 'server' ? 'Server' : s;
        const sColor = s === 'sidecar' ? 'text-blue-400 border-blue-400/30' : s === 'config' ? 'text-amber-400 border-amber-400/30' : 'text-zinc-500 border-zinc-500/30';
        sourceBadge = `<span class="text-[8px] px-1 py-px rounded border ${sColor} uppercase tracking-tighter ml-1.5">${escapeHTML(label)}</span>`;
    }

    const accountHTML = accounts.length === 1
        ? `<div class="flex items-center text-[10px] text-zinc-500 mt-0.5 min-w-0"><span class="truncate">${escapeHTML(accounts[0])}</span>${sourceBadge}</div>`
        : accounts.length > 1
        ? `<div class="flex flex-wrap gap-1 mt-1">${accounts.map(a =>
            `<span class="text-[9px] bg-zinc-800 text-zinc-500 px-1.5 py-0.5 rounded">${escapeHTML(a)}</span>`
          ).join('')}</div>`
        : '';

    // Health badge (CRIT / WARN / GOOD / UNLM)
    const badgeLabels = { critical: 'CRIT', warning: 'WARN', good: 'GOOD', unlimited: 'UNLM', unknown: '——' };
    const healthBadgeHTML = `<span class="text-sm font-bold px-1.5 py-0.5 rounded border ${h.badge} border-current/30">${badgeLabels[worst.health] || '——'}</span>`;

    // Segmented bar (per-service health counts)
    const barCounts = { critical: 0, warning: 0, good: 0, unlimited: 0 };
    items.forEach(i => { if (barCounts[i.health] !== undefined) barCounts[i.health]++; });
    const BAR_HEX = { critical: '#ef4444', warning: '#eab308', good: '#22c55e', unlimited: '#8b5cf6' };
    const barSegments = Object.entries(barCounts)
        .filter(([, c]) => c > 0)
        .map(([k, c]) => `<div style="flex:${c};background:${BAR_HEX[k]};"></div>`)
        .join('');

    // Per-service breakdown rows (draggable in edit mode — see data-card-key).
    const breakdownRows = ordered.map(item => {
        const dot = HEALTH_CONFIG[item.health]?.dot || 'dot-unknown';
        let pct = null;
        if (!item.is_unlimited && item.used_value != null && item.limit_value > 0) {
            pct = (item.used_value / item.limit_value) * 100;
        }
        const display = item.is_unlimited ? '∞' : pct != null ? `${pct.toFixed(0)}%` : escapeHTML(String(item.remaining ?? '—'));
        const rowTier = item.tier ? `<span class="text-[8px] font-bold px-1 py-px rounded border ${getTierTextClass(item.tier)} border-current/30 flex-shrink-0">${escapeHTML(item.tier.toUpperCase())}</span>` : '';
        return `<div class="flex justify-between items-center text-xs py-0.5" data-card-key="${escapeHTMLAttr(cardKey(item))}">
            <span class="flex items-center gap-1.5 min-w-0">
                <span class="dot ${dot} flex-shrink-0" style="width:6px;height:6px;"></span>
                <span class="text-zinc-300 truncate">${escapeHTML(item.service_name)}</span>
            </span>
            <span class="flex items-center gap-1.5 text-zinc-500 flex-shrink-0 ml-2">${rowTier}<span class="mono text-right" style="min-width:2.5rem">${display}</span></span>
        </div>`;
    }).join('');

    return `<div class="glass-panel ${h.card} rounded-2xl overflow-hidden cursor-pointer select-none hover:scale-[1.01] active:scale-[0.99] transition-all duration-200 card-layout relative"
         data-provider-id="${escapeHTMLAttr(providerId)}"
         onclick="openProviderModal('${escapeHTMLAttr(providerId)}')">
        <span class="drag-handle" aria-hidden="true" onclick="event.stopPropagation()">
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                <circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/>
                <circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/>
                <circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/>
            </svg>
        </span>
        <div class="p-5">
            <div class="flex justify-between items-start mb-1">
                <div class="min-w-0 flex-1">
                    <div class="flex items-center gap-2 flex-wrap">
                        <span class="text-xs font-bold text-zinc-400 uppercase tracking-widest">${icon} ${escapeHTML(providerId)}</span>
                        ${tierBadgeHTML}
                    </div>
                    ${accountHTML}
                </div>
                <div class="ml-2 flex-shrink-0">${healthBadgeHTML}</div>
            </div>
            <div class="text-4xl font-black ${worstColor} leading-none mt-3">${worstDisplay}</div>
            <div class="text-xs text-zinc-500 mt-1.5">${escapeHTML(worst.service_name)} · worst</div>
        </div>
        <div class="border-t border-zinc-800/50 bg-black/20 px-5 py-3.5 flex flex-col justify-start">
            <div class="h-0.5 rounded-full overflow-hidden flex gap-0.5 mb-3">${barSegments}</div>
            <div class="space-y-1" data-subitems-for="${escapeHTMLAttr(providerId)}">${breakdownRows}</div>
        </div>
    </div>`;
}

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
        <div class="bg-zinc-950 border border-zinc-800/60 rounded-xl p-4">
            <div class="flex justify-between items-start mb-2.5">
                <div class="flex-1 min-w-0">
                    <div class="skeleton h-6 w-32 mb-2" style="border-radius: 6px;"></div>
                    <div class="flex gap-2">
                        <div class="skeleton h-4 w-16" style="border-radius: 6px;"></div>
                        <div class="skeleton h-4 w-12" style="border-radius: 6px;"></div>
                    </div>
                </div>
                <div class="skeleton w-16 h-7" style="border-radius: 6px;"></div>
            </div>
            <div class="skeleton h-5 w-full mb-2" style="border-radius: 6px;"></div>
            <div class="h-1 bg-zinc-800 rounded-full overflow-hidden">
                <div class="skeleton h-full w-3/4" style="border-radius: 0;"></div>
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

    const BAR_HEX = { critical: '#ef4444', warning: '#eab308', good: '#22c55e', unlimited: '#8b5cf6', unknown: '#3f3f46' };
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

        // Sparkline — filter history for this service
        const svcHistory = (history || [])
            .filter(s => s.provider_id === providerId && s.service_name === item.service_name && typeof s.used_value === 'number' && isFinite(s.used_value))
            .sort((a, b) => a.timestamp.localeCompare(b.timestamp))
            .map(s => ({ value: s.used_value }));
        const sparkColor = item.is_unlimited ? '#8b5cf6' : barColor;
        const sparkSVG = buildSparklineSVG(svcHistory, sparkColor);
        const trendArrow = getTrendArrow(svcHistory);

        // Used / limit display
        const fmt = formatUsageValues(item.used_value, item.limit_value, item.unit_type, item.currency);
        const usageText = item.is_unlimited
            ? 'Unlimited'
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

        return `<div class="bg-zinc-950 border border-zinc-800/60 rounded-xl p-4 relative" data-card-key="${escapeHTMLAttr(cardKey(item))}">
            <span class="drag-handle" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                    <circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/>
                    <circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/>
                    <circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/>
                </svg>
            </span>
            <div class="flex justify-between items-start mb-2.5">
                <div class="flex-1 min-w-0">
                    <div class="text-2xl font-bold text-zinc-100">${escapeHTML(item.service_name)}</div>
                    <div class="flex flex-wrap items-center gap-1.5 mt-1.5">
                        <span class="text-sm font-bold px-1.5 py-0.5 rounded border leading-none uppercase tracking-widest mono ${h.badge} border-current/30">${badgeLabels[item.health] || '——'}</span>
                        ${tierBadge}
                        ${combinedSourceLabel ? `<span class="text-sm text-zinc-500">${combinedSourceLabel}</span>` : ''}
                        ${paceIcon ? `<span class="text-2xl">${paceIcon}</span>` : ''}
                        ${item.pace ? `<span class="text-sm text-zinc-500">${escapeHTML(item.pace)}</span>` : ''}
                    </div>
                </div>
                <div class="flex items-center gap-2 flex-shrink-0 ml-3">
                    <span class="text-2xl text-zinc-400">${trendArrow}</span>
                    ${sparkSVG}
                </div>
            </div>
            <div class="flex justify-between text-xl text-zinc-400 mb-2">
                <span class="font-medium">${usageText}</span>
                <span>${resetText}</span>
            </div>
            <div class="h-1 bg-zinc-800 rounded-full overflow-hidden">
                <div class="h-full rounded-full transition-all" style="width:${Math.min(barWidth, 100).toFixed(1)}%;background:${barColor};"></div>
            </div>
        </div>`;
    }).join('');

    return `<div>
        <div class="flex justify-between items-start mb-5 pb-4 border-b border-zinc-800/50">
            <div>
                <div class="text-xl font-black text-zinc-100">${icon} ${escapeHTML(providerId)}</div>
                <div class="text-sm text-zinc-500 mt-1">${escapeHTML(accountText)}${windowType ? ' · ' + escapeHTML(windowType) : ''} · ${serviceCount} service${serviceCount !== 1 ? 's' : ''}</div>
            </div>
            <div class="flex items-center gap-1">
                <button id="refresh-provider-btn" title="Refresh now" class="w-8 h-8 flex items-center justify-center rounded-full hover:bg-zinc-800 transition-colors text-zinc-400 hover:text-zinc-100">
                    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
                </button>
                <button id="close-modal" class="text-zinc-400 hover:text-zinc-200 transition-colors text-xl leading-none w-8 h-8 flex items-center justify-center rounded-full hover:bg-zinc-800">✕</button>
            </div>
        </div>
        <div class="space-y-3 max-h-[65vh] overflow-y-auto pr-1" data-provider-id="${escapeHTMLAttr(providerId)}">${serviceRows}</div>
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
        const icon = PROVIDER_ICONS[pid] || '🔧';
        const color = PROVIDER_HEX[pid] || '#64748b';
        const isActive = !activeProviders || activeProviders.has(pid);

        // Build series averaged per bucket sized to match the active window.
        const byBucket = new Map();
        for (const s of snaps) {
            const t = Math.floor(new Date(s.timestamp).getTime() / 1000);
            const bucket = t - (t % bucketSeconds);
            if (!byBucket.has(bucket)) byBucket.set(bucket, { sum: 0, count: 0 });
            const b = byBucket.get(bucket);
            b.sum += s.used_value;
            b.count += 1;
        }
        const points = [...byBucket.entries()]
            .sort(([a], [b]) => a - b)
            .map(([, b]) => ({ value: b.sum / b.count }));

        const sparkSVG = buildSparklineSVG(points, color, 56, 24);
        const trendArrow = getTrendArrow(points);
        const latestValue = points.length > 0 ? points[points.length - 1].value.toFixed(0) : '—';
        const trendColor = trendArrow === '↑' ? 'text-red-400' : trendArrow === '↓' ? 'text-green-400' : 'text-zinc-400';
        const activeOpacity = isActive ? '' : 'opacity-40';

        return `<div class="glass-panel border rounded-xl p-3 cursor-pointer select-none hover:opacity-90 transition-all ${activeOpacity}"
                     style="border-color:${isActive ? color : '#27272a'};"
                     onclick="toggleHistoryProvider('${escapeHTMLAttr(pid)}')">
            <div class="flex items-center justify-between gap-2 mb-1.5">
                <span class="text-[9px] font-bold text-zinc-400 uppercase tracking-wide">${icon} ${escapeHTML(pid)}</span>
                ${sparkSVG}
            </div>
            <div class="flex items-baseline gap-1">
                <span class="text-sm font-black text-zinc-100">${latestValue}</span>
                <span class="text-[10px] ${trendColor} font-bold">${trendArrow}</span>
            </div>
        </div>`;
    }).join('');

    return `<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-4">${cards}</div>`;
}
