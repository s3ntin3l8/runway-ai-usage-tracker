// Warms a lazy route's JS chunk before the user actually navigates to it —
// on nav-link hover/focus/touchstart, not on click. Without this, every
// route switch is a serialized "click → fetch chunk → parse → mount → fetch
// data" waterfall; this collapses the chunk-fetch step into the dwell time
// before the click, which is usually enough to have it already cached.
//
// Import specifiers below must match `app.tsx`'s `lazy(() => import(...))`
// calls exactly (identical string literal) so Vite/Rollup resolves them to
// the same chunk — calling import() again here just returns the in-flight
// or already-resolved module promise, it doesn't fetch a second time.
// Home isn't listed: it's the one eager route (see app.tsx), already in the
// initial bundle. Provider isn't listed: it's reached by clicking a card,
// not a nav link, so there's no hover target to hang a prefetch off of.

const PREFETCHERS: Record<string, () => Promise<unknown>> = {
  '/insights': () => import('@/features/insights/InsightsPage'),
  '/history': () => import('@/features/history/HistoryPage'),
  '/fleet': () => import('@/features/fleet/FleetPage'),
  '/settings': () => import('@/features/settings/SettingsPage'),
};

const prefetched = new Set<string>();

/** Trigger the dynamic import for `to`, at most once per route per session.
 * Safe to call from every hover/focus/touchstart — a no-op past the first
 * call for a given route, and a no-op for routes with no prefetcher. */
export function prefetchRoute(to: string): void {
  const prefetch = PREFETCHERS[to];
  if (!prefetch || prefetched.has(to)) return;
  prefetched.add(to);
  void prefetch();
}
