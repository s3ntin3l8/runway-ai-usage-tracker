import { NAV_ITEMS, isNavItemActive } from './nav';

const item = (to: string) => NAV_ITEMS.find((i) => i.to === to)!;

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

describe('isNavItemActive', () => {
  it('keeps Home active on the provider drill-down', () => {
    const home = item('/');
    expect(isNavItemActive(home, '/')).toBe(true);
    expect(isNavItemActive(home, '/provider/anthropic')).toBe(true);
  });

  it('does not light an end-matched item up for anything else', () => {
    const home = item('/');
    expect(isNavItemActive(home, '/insights')).toBe(false);
    expect(isNavItemActive(home, '/settings/providers')).toBe(false);
  });

  it('keeps a parent active for its descendants', () => {
    const settings = item('/settings');
    expect(isNavItemActive(settings, '/settings')).toBe(true);
    expect(isNavItemActive(settings, '/settings/providers')).toBe(true);
    expect(isNavItemActive(settings, '/fleet')).toBe(false);
  });
});
