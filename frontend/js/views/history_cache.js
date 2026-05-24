// Pure helpers for the history-view stale-while-revalidate cache.
// Extracted from history.js so they can be unit-tested under plain Node
// without DOM/Storage stubs.

// 45s strikes a balance: short enough that long-stale data converges back to
// fresh on its own; long enough to feel instant when the user mashes the
// timeframe buttons.
export const CACHE_TTL_MS = 45_000;

export function _cacheKey({ metric, windowFilter, providers }) {
    return `${metric}|${windowFilter || 'all'}|${providers ?? '*'}`;
}

// Returns the cache entry only if it is fresh AND covers at least `requestedDays`.
// A 30d superset can serve a 7d view by filtering points; a 7d cache cannot
// answer a 30d question, so we return null in that direction.
export function _cacheHit(slot, key, requestedDays, now = Date.now()) {
    const e = slot[key];
    if (!e) return null;
    if (now - e.fetchedAt > CACHE_TTL_MS) return null;
    if (e.days < requestedDays) return null;
    return e;
}

export function _filterChartByDays(response, days, now = Date.now()) {
    const cutoff = now - days * 86400_000;
    if (response.series) {
        return {
            ...response,
            series: response.series.map((s) => ({
                ...s,
                points: (s.points || []).filter((p) => Date.parse(p.ts) >= cutoff),
            })),
        };
    }
    if (response.bars) {
        return { ...response, bars: response.bars.filter((b) => Date.parse(b.ts) >= cutoff) };
    }
    return response;
}

export function _filterSnapshotsByDays(response, days, now = Date.now()) {
    // Preserve {total, page, limit} from the superset response. Pagination
    // clicks bypass the cache, so the filtered total is informational only.
    const cutoff = now - days * 86400_000;
    return { ...response, rows: response.rows.filter((r) => Date.parse(r.ts) >= cutoff) };
}
