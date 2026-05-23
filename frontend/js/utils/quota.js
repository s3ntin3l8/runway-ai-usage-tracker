/**
 * Shared quota clustering utilities.
 * Used by fleet-commander.js (pool bars) and dashboard.js (hero card label).
 */

/** Tolerance for "same physical quota" detection. */
export const SAME_QUOTA_PCT_EPSILON = 0.6;   // pct_used must agree within 0.6%
export const SAME_QUOTA_RESET_MS    = 90000; // reset_at must agree within 90 seconds
// 90 s is intentionally generous — Antigravity model cards share the exact same
// reset_at, but floating-point Date parsing can introduce millisecond jitter.

/** Raw pct_used for a card without rounding — null when unavailable. */
function _cardPct(card) {
    if (card.pct_used != null) return card.pct_used;
    if (card.used_value != null && card.limit_value) {
        return (card.used_value / card.limit_value) * 100;
    }
    return null;
}

/**
 * Returns true when cards a and b appear to share the same physical quota
 * (same window_type, pct within epsilon, reset_at within 90 s).
 * @param {object} a
 * @param {object} b
 * @returns {boolean}
 */
export function sameQuota(a, b) {
    if ((a.window_type || '') !== (b.window_type || '')) return false;
    const aPct = _cardPct(a), bPct = _cardPct(b);
    if (aPct == null || bPct == null) return false;
    if (Math.abs(aPct - bPct) > SAME_QUOTA_PCT_EPSILON) return false;
    if (!a.reset_at || !b.reset_at) return false;
    return Math.abs(new Date(a.reset_at) - new Date(b.reset_at)) <= SAME_QUOTA_RESET_MS;
}

/**
 * Cluster an array of cards by shared physical quota.
 * Returns an array of clusters (each cluster is an array of cards).
 * Singletons have length 1.
 * @param {object[]} cards
 * @returns {object[][]}
 */
export function clusterPools(cards) {
    const clusters = [];
    const seen = new Set();
    for (const card of cards) {
        if (seen.has(card)) continue;
        const cluster = [card];
        seen.add(card);
        for (const other of cards) {
            if (seen.has(other)) continue;
            if (sameQuota(card, other)) {
                cluster.push(other);
                seen.add(other);
            }
        }
        clusters.push(cluster);
    }
    return clusters;
}

/**
 * Build a compact display label listing a cluster's model names.
 * Strips "AG: " provider prefix noise. Truncates to "+N" form beyond 2 names
 * to keep the label narrow enough to fit a single pool row / hero quota line.
 * @param {object[]} cluster
 * @returns {string}
 */
export function clusterModelLabel(cluster) {
    const names = cluster
        .map(c => (c.service_name || c.model_id || '').replace(/^AG:\s*/i, ''))
        .filter(Boolean);
    if (names.length <= 2) return names.join(' · ');
    return names.slice(0, 2).join(' · ') + ` +${names.length - 2}`;
}
