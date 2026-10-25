// Shared helpers for the UX suite.
//
// The suite runs against the demo instance (see `playwright.ux.config.ts`),
// which serves the built SPA on one port with seeded data.

export const DEMO_URL = 'http://127.0.0.1:3965';

/** A phone. The audit's mobile findings are all about this width. */
export const PHONE = { width: 390, height: 844 };

/**
 * The routes that carry content on a phone. `/settings` is a list of
 * sections on a phone and one section on a desktop, so both are worth a
 * look; the provider page is the densest surface the app has.
 */
export const PAGES = [
  { path: '/', name: 'home' },
  { path: '/insights', name: 'insights' },
  { path: '/history', name: 'history' },
  { path: '/fleet', name: 'fleet' },
  { path: '/settings', name: 'settings' },
  { path: '/provider/anthropic', name: 'provider' },
];

/**
 * Wait for the shell, not for the network to go quiet.
 *
 * The app polls every couple of minutes, so `networkidle` may never settle;
 * the page's `<main>` is there as soon as the route has rendered.
 */
export async function waitForApp(page: import('@playwright/test').Page): Promise<void> {
  await page.locator('main').first().waitFor({ state: 'attached' });
  await page.waitForTimeout(400);
}

/** The width of the scrollbar-free viewport the page lays out against. */
export const SLACK = 1;
