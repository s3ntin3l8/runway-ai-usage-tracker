import { HEALTH_CONFIG, STATE } from './state.js';

/**
 * @typedef {Object} LimitCard
 * @property {string} service - Service name (e.g., "Claude Pro")
 * @property {string} icon - Emoji icon representing the service
 * @property {string} remaining - Remaining capacity (number, percentage, or "ERR")
 * @property {string} unit - Unit of measurement (e.g., "tokens / 5h", "capacity", "%")
 * @property {string} reset - Human-readable time until reset (e.g., "in 2h 30m")
 * @property {string} health - Health status ("good", "warning", "critical", "unknown")
 * @property {string} pace - Burn rate descriptor (e.g., "Sustainable", "Fast Burn")
 * @property {string} detail - Additional details (e.g., usage percentage, last update time)
 */

/**
 * Parse percentage value from detail string
 * @param {string} detail - Detail string that may contain a percentage
 * @returns {number|null} Parsed percentage (0-100) or null if not found
 */
function parseProgressPct(detail) {
    const m = detail.match(/(\d+(\.\d+)?)%/);
    return m ? Math.min(100, parseFloat(m[1])) : null;
}

/**
 * Build an HTML card element for a limit
 * @param {LimitCard} item - The limit card data
 * @returns {string} HTML string representing the card
 */
export function buildCard(item) {
    const h = HEALTH_CONFIG[item.health] || HEALTH_CONFIG.unknown;
    const usedPct = parseProgressPct(item.detail || '');
    
    // Inverted Logic: show remaining capacity instead of used
    let barWidth = usedPct;
    if (STATE.remaining && usedPct !== null) {
        barWidth = 100 - usedPct;
    }

    const isPlaceholder = item.health === 'unknown';

    const progressBar = barWidth !== null ? `
        <div class="progress-track mt-4">
            <div class="progress-fill" style="width: ${barWidth}%; background: ${h.bar};"></div>
        </div>` : '';

    const detailEl = item.detail ? `
        <p class="text-xs text-zinc-600 mono mt-1 truncate" title="${item.detail}">${item.detail}</p>` : '';

    const paceBadge = item.pace ? `
        <span class="text-[10px] font-bold text-zinc-500 bg-zinc-900/80 border border-zinc-800 px-1.5 py-0.5 rounded-full mono">${item.pace}</span>
    ` : '';

    return `
        <div class="glass-panel ${h.card} rounded-2xl p-5 relative flex flex-col gap-3">
            <!-- Header row -->
            <div class="flex items-start justify-between gap-2">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="text-xl leading-none">${item.icon}</span>
                    <div class="flex flex-col">
                        <span class="text-[10px] font-semibold text-zinc-400 uppercase tracking-wide truncate">${item.service}</span>
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
                    <span class="text-4xl font-black tracking-tighter ${isPlaceholder ? 'text-zinc-600' : 'text-zinc-50'}">${item.remaining}</span>
                    <span class="text-sm font-medium text-zinc-500">${item.unit}</span>
                </div>
                ${detailEl}
            </div>

            <!-- Reset footer -->
            <div class="mt-auto pt-3 border-t border-zinc-800/60 flex items-center justify-between">
                <span class="text-xs text-zinc-600 mono font-medium">RESETS</span>
                <span class="text-xs font-semibold text-zinc-400 bg-zinc-800/60 px-2 py-1 rounded-md mono">
                    ${item.reset}
                </span>
            </div>
        </div>
    `;
}
