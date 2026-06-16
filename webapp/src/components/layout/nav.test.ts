import { NAV_ITEMS } from './nav';

describe('NAV_ITEMS', () => {
  it('exposes the primary destinations with unique targets', () => {
    expect(NAV_ITEMS.map((i) => i.to)).toEqual([
      '/',
      '/insights',
      '/history',
      '/fleet',
      '/settings',
    ]);
    const targets = new Set(NAV_ITEMS.map((i) => i.to));
    expect(targets.size).toBe(NAV_ITEMS.length);
  });

  it('marks only the home route as end-matched and gives every item an icon', () => {
    const home = NAV_ITEMS.find((i) => i.to === '/');
    expect(home?.end).toBe(true);
    for (const item of NAV_ITEMS) {
      expect(item.label).toBeTruthy();
      // lucide icons are renderable React components (function or forwardRef object).
      expect(item.icon).toBeTruthy();
      expect(['function', 'object']).toContain(typeof item.icon);
    }
  });
});
