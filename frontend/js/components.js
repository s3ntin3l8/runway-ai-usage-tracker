import { HEALTH_CONFIG, STATE, ERROR_TYPES } from './state.js';

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
 * Returns a styled tier badge based on the tier name
 * @param {string} tier - Tier name (Free, Pro, Team, etc.)
 * @returns {string} HTML for the badge, or empty string if no tier
 */
function getTierBadge(tier) {
    // Don't show badge if tier is null, undefined, or empty
    if (tier === null || tier === undefined || tier === '') return '';
    const t = tier.toLowerCase();
    let classes = 'bg-zinc-800/50 text-zinc-500 border-zinc-700/50'; // Default/Free

    if (t.includes('pro') || t.includes('premium') || t.includes('plus')) {
        classes = 'bg-amber-500/10 text-amber-400 border-amber-500/20';
    } else if (t.includes('team') || t.includes('enterprise') || t.includes('organization')) {
        classes = 'bg-violet-500/10 text-violet-400 border-violet-500/20';
    } else if (t.includes('free')) {
        classes = 'bg-zinc-800/50 text-zinc-500 border-zinc-700/50';
    }

    return `<span class="text-[8px] font-bold px-1 py-0.5 rounded border leading-none uppercase tracking-tighter ${classes}">${escapeHTML(tier)}</span>`;
}

/**
 * @typedef {Object} LimitCard
 * @property {string} service - Service name (e.g., "Claude Pro")
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
 * Format reset time tooltip with absolute time
 * @param {string|null} resetAt - ISO 8601 timestamp
 * @returns {string|null} Formatted tooltip text or null
 */
function formatResetTooltip(resetAt) {
    if (!resetAt || resetAt === '—') return null;

    try {
        const date = new Date(resetAt);
        const now = new Date();

        // Check if date is valid
        if (isNaN(date.getTime())) return null;

        const diffHours = (date - now) / (1000 * 60 * 60);

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
        if (diffHours >= 24) {
            return `Resets at ${timeStr} on ${dateStr}`;
        } else {
            return `Resets at ${timeStr}`;
        }
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
    const isDisabled = STATE.disabledServices.includes(item.service);
    if (isDisabled && !STATE.showHidden) return '';

    // Handle Compact Mode
    if (STATE.compact) {
        let mainDisplay = '';
        if (isUnlimited) {
            mainDisplay = `<span class="text-2xl font-black tracking-tighter text-violet-400 leading-none">∞</span>`;
        } else if (hasPercentage) {
            mainDisplay = `<span class="text-2xl font-black tracking-tighter ${isPlaceholder ? 'text-zinc-600' : 'text-zinc-50'} leading-none">${displayPct.toFixed(1)}%</span>`;
        } else {
            mainDisplay = `<span class="text-2xl font-black tracking-tighter ${isPlaceholder ? 'text-zinc-600' : 'text-zinc-50'} leading-none">${escapeHTML(item.remaining)}</span>`;
        }

        return `
            <div class="glass-panel ${h.card} ${isDisabled ? 'disabled-card' : ''} rounded-xl p-3 relative flex flex-col gap-2 cursor-pointer select-none active:scale-[0.98] transition-all duration-200" data-service="${escapeHTML(item.service)}">
                <div class="flex items-center justify-between gap-2">
                    <div class="flex items-center gap-1.5 min-w-0">
                        <span class="text-base leading-none">${escapeHTML(item.icon)}</span>
                        <span class="text-[10px] font-bold text-zinc-400 uppercase tracking-tight truncate">${escapeHTML(item.service)}</span>
                    </div>
                    <div class="dot ${h.dot} shrink-0"></div>
                </div>

                <div class="flex items-end justify-between gap-1 mt-1">
                    <div class="flex items-baseline gap-1">
                        ${mainDisplay}
                        ${!isUnlimited && hasPercentage ? `<span class="text-[8px] font-bold text-zinc-500 uppercase">${displayLabel}</span>` : ''}
                    </div>
                    <span class="text-[9px] text-zinc-500 mono leading-none mb-0.5 truncate max-w-[80px]" title="${escapeHTML(item.reset)}">${escapeHTML(item.reset)}</span>
                </div>

                <div class="progress-track h-1 mt-auto overflow-hidden rounded-full bg-zinc-800/50 ${isUnlimited ? 'progress-unlimited' : ''}">
                    <div class="progress-fill h-full" style="width: ${isUnlimited ? 100 : barWidth}%; background: ${isUnlimited ? 'linear-gradient(90deg, #ff0080, #ff8c00, #40e0d0)' : h.bar};"></div>
                </div>
            </div>
        `;
    }

    // Standard Layout (Below)
    displayLabel = STATE.remaining ? 'remaining' : 'used';

    // Build subtitle with raw values and data source
    let subtitle = '';
    const sourceLabel = formatDataSource(item.data_source);
    if (isUnlimited) {
        subtitle = `<span class="text-xs text-zinc-500">No usage limit${sourceLabel}</span>`;
    } else if (hasPercentage && item.used_value !== null && item.limit_value !== null) {
        const displayValue = STATE.remaining ? Math.max(0, item.limit_value - item.used_value) : item.used_value;
        const formatted = formatUsageValues(
            displayValue,
            item.limit_value,
            item.unit_type || 'generic',
            item.currency
        );
        subtitle = `<span class="text-xs text-zinc-500">${escapeHTML(formatted.used)} of ${escapeHTML(formatted.limit)} ${escapeHTML(formatted.unit)} ${displayLabel}${sourceLabel}</span>`;
    } else if (item.detail) {
        // Fallback to detail field
        const escapedDetail = escapeHTML(item.detail);
        subtitle = `<span class="text-xs text-zinc-600 mono truncate" title="${escapedDetail}">${errorIcon}${escapedDetail}${sourceLabel}</span>`;
    }

    // Progress bar with appropriate styling
    let progressBarClass = 'progress-fill';
    let progressTrackClass = 'progress-track mt-3';

    if (isUnlimited) {
        progressTrackClass += ' progress-unlimited';
    }

    const progressBar = hasPercentage || isUnlimited ? `
        <div class="${progressTrackClass}">
            <div class="${progressBarClass}" style="width: ${isUnlimited ? 100 : barWidth}%; background: ${isUnlimited ? 'linear-gradient(90deg, #ff0080, #ff8c00, #40e0d0)' : h.bar};"></div>
        </div>` : '';

    // Pace badge
    const paceBadge = item.pace ? `
        <span class="text-[10px] font-bold text-zinc-500 bg-zinc-900/80 border border-zinc-800 px-1.5 py-0.5 rounded-full mono">${escapeHTML(item.pace)}</span>
    ` : '';

    // Main display value
    let mainDisplay = '';
    if (isUnlimited) {
        mainDisplay = `<span class="text-4xl font-black tracking-tighter text-violet-400">∞</span>`;
    } else if (hasPercentage) {
        mainDisplay = `<span class="text-4xl font-black tracking-tighter ${isPlaceholder ? 'text-zinc-600' : 'text-zinc-50'}">${displayPct.toFixed(1)}%</span>`;
    } else {
        // Fallback to remaining value
        mainDisplay = `<span class="text-4xl font-black tracking-tighter ${isPlaceholder ? 'text-zinc-600' : 'text-zinc-50'}">${escapeHTML(item.remaining)}</span>
                <span class="text-sm font-medium text-zinc-500">${escapeHTML(item.unit)}</span>`;
    }

    // For unlimited plans, add unit label next to infinity
    const unitLabel = isUnlimited ? `<span class="text-sm font-medium text-zinc-500 ml-2">${escapeHTML(item.unit || 'Unlimited')}</span>` : '';

    // Build reset element with tooltip
    const resetTooltip = formatResetTooltip(item.reset_at);
    const resetElement = resetTooltip ? `
        <div class="tooltip-container">
            <span class="text-xs font-semibold text-zinc-400 bg-zinc-800/60 px-2 py-1 rounded-md mono cursor-help">
                ${escapeHTML(item.reset)}
            </span>
            <div class="tooltip">${resetTooltip}</div>
        </div>
    ` : `<span class="text-xs font-semibold text-zinc-400 bg-zinc-800/60 px-2 py-1 rounded-md mono">${escapeHTML(item.reset)}</span>`;

    return `
        <div class="glass-panel ${h.card} ${isDisabled ? 'disabled-card' : ''} rounded-2xl p-5 relative flex flex-col gap-3 cursor-pointer select-none active:scale-[0.98] transition-all duration-200" data-service="${escapeHTML(item.service)}">
            <!-- Header row -->
            <div class="flex items-start justify-between gap-2">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="text-xl leading-none">${escapeHTML(item.icon)}</span>
                    <div class="flex flex-col">
                        <div class="flex items-center gap-1.5">
                            <span class="text-[10px] font-semibold text-zinc-400 uppercase tracking-wide truncate">${escapeHTML(item.service)}</span>
                            ${getTierBadge(item.tier)}
                        </div>
                        ${paceBadge}
                    </div>
                </div>
                <div class="flex items-center gap-1.5 shrink-0">
                    <div class="dot ${h.dot}"></div>
                    <span class="text-xs font-bold ${h.badge} mono">${h.label}</span>
                </div>
            </div>

            ${progressBar}

            <!-- Main value -->
            <div class="mt-1">
                <div class="flex items-baseline gap-1.5 flex-wrap">
                    ${mainDisplay}${unitLabel}
                </div>
                <div class="mt-1">
                    ${subtitle}
                </div>
            </div>

            <!-- Reset footer -->
            <div class="mt-auto pt-3 border-t border-zinc-800/60 flex items-center justify-between">
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

    const usedPct = calculateUsedPct(item);
    const isDisabled = STATE.disabledServices.includes(item.service);

    const formatted = formatUsageValues(
        item.used_value,
        item.limit_value,
        item.unit_type || 'generic',
        item.currency
    );

    const sourceLabel = SOURCE_LABELS[item.data_source] || item.data_source;
    const sourceColor = SOURCE_COLORS[item.data_source] || 'text-zinc-400';
    const resetTime = item.reset_at ? new Date(item.reset_at).toLocaleString() : 'Never';

    return `
        <div class="modal-header border-b border-zinc-800/80">
            <div class="flex items-center justify-between mb-4">
                <div class="flex items-center gap-3">
                    <span class="text-3xl">${escapeHTML(item.icon)}</span>
                    <div>
                        <h2 class="text-xl font-black text-zinc-50 tracking-tight">${escapeHTML(item.service)}</h2>
                        <div class="flex items-center gap-2 mt-0.5">
                            <span class="text-xs font-bold ${h.badge} mono uppercase tracking-widest">${h.label}</span>
                            ${getTierBadge(item.tier)}
                        </div>
                    </div>
                </div>
                <button id="close-modal" class="w-10 h-10 flex items-center justify-center rounded-full hover:bg-zinc-800 transition-colors text-zinc-400">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                </button>
            </div>
            
            <div class="mt-6 flex flex-col gap-2">
                <div class="flex items-baseline justify-between">
                    <span class="text-5xl font-black tracking-tighter text-zinc-50">
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
                <span class="text-[10px] font-bold text-zinc-500 uppercase tracking-widest">Usage Value</span>
                <span class="text-sm font-semibold text-zinc-200 mono">${escapeHTML(formatted.used)}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span class="text-[10px] font-bold text-zinc-500 uppercase tracking-widest">Service Limit</span>
                <span class="text-sm font-semibold text-zinc-200 mono">${isUnlimited ? '∞' : escapeHTML(formatted.limit)} ${escapeHTML(formatted.unit)}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span class="text-[10px] font-bold text-zinc-500 uppercase tracking-widest">Resets At</span>
                <span class="text-sm font-semibold text-zinc-200 mono">${resetTime}</span>
            </div>
            <div class="modal-detail-item flex flex-col gap-1">
                <span class="text-[10px] font-bold text-zinc-500 uppercase tracking-widest">Data Source</span>
                <span class="text-sm font-bold ${sourceColor} mono">● ${sourceLabel}</span>
            </div>
        </div>

        ${item.detail ? `
        <div class="mt-6 p-4 rounded-2xl bg-black/40 border border-zinc-800/60">
            <span class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest block mb-2">Technical Summary</span>
            <p class="text-xs text-zinc-400 mono leading-relaxed break-all">${escapeHTML(item.detail)}</p>
        </div>
        ` : ''}

        ${item.pace ? `
        <div class="mt-4 flex items-center justify-between px-1">
            <span class="text-[10px] font-bold text-zinc-600 uppercase tracking-widest">Consumption Rate</span>
            <span class="text-xs font-bold text-zinc-400 mono">${escapeHTML(item.pace)}</span>
        </div>
        ` : ''}

        <div class="mt-8 pt-6 border-t border-zinc-800/80 flex items-center justify-between">
            <div class="flex flex-col gap-1">
                <span class="text-sm font-bold text-zinc-200">Show in Dashboard</span>
                <span class="text-[10px] text-zinc-500 uppercase tracking-wider font-medium">Hide if not in use</span>
            </div>
            <button 
                onclick="event.stopPropagation(); window.toggleService('${escapeHTML(item.service)}')"
                class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${!isDisabled ? 'bg-blue-600' : 'bg-zinc-700'}"
            >
                <span class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${!isDisabled ? 'translate-x-5' : 'translate-x-0'}"></span>
            </button>
        </div>
    `;
}
