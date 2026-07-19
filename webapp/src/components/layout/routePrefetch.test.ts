import { prefetchRoute } from './routePrefetch';

// Stub the lazy route targets so triggering their dynamic import in tests
// doesn't pull in real page components (queries, charts, etc).
vi.mock('@/features/insights/InsightsPage', () => ({ InsightsPage: () => null }));
vi.mock('@/features/history/HistoryPage', () => ({ HistoryPage: () => null }));
vi.mock('@/features/fleet/FleetPage', () => ({ FleetPage: () => null }));
vi.mock('@/features/settings/SettingsPage', () => ({ SettingsPage: () => null }));

describe('prefetchRoute', () => {
  it('does not throw for every known nav route', () => {
    for (const to of ['/insights', '/history', '/fleet', '/settings']) {
      expect(() => prefetchRoute(to)).not.toThrow();
    }
  });

  it('is a no-op for a route with no prefetcher (Home, Provider, unknown paths)', () => {
    expect(() => prefetchRoute('/')).not.toThrow();
    expect(() => prefetchRoute('/provider/anthropic')).not.toThrow();
    expect(() => prefetchRoute('/nope')).not.toThrow();
  });

  it('is idempotent — repeated calls for the same route do not throw', () => {
    expect(() => {
      prefetchRoute('/insights');
      prefetchRoute('/insights');
      prefetchRoute('/insights');
    }).not.toThrow();
  });
});
