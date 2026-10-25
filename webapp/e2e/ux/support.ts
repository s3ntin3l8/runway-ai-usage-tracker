// Shared helpers for the UX suite.
//
// The suite runs against the demo instance (see `playwright.ux.config.ts`),
// which serves the built SPA on one port with seeded data.

import type { Page } from '@playwright/test';

export const DEMO_URL = 'http://127.0.0.1:3965';

/** A phone. The audit's mobile findings are all about this width. */
export const PHONE = { width: 390, height: 844 };

/**
 * The routes that carry content on a phone, and the seeded value each one
 * has to show before it is measured.
 *
 * `/settings` is a list of sections on a phone and one section on a
 * desktop, so both are worth a look; the provider page is the densest
 * surface the app has.
 *
 * The marker is what makes the suite honest, and every marker is a value
 * the seed produces: a provider's display name, a model count, the seeded
 * account's address, a sidecar's name. None of them is a number that moves
 * with the date. The settings page has no usage data of its own -- its
 * content IS the section list, and the first section in it is the thing the
 * audit found missing.
 */
export const PAGES = [
  { path: '/', name: 'home', marker: 'ChatGPT' },
  { path: '/insights', name: 'insights', marker: 'Top 8 models' },
  { path: '/history', name: 'history', marker: 'dev@example.com' },
  { path: '/fleet', name: 'fleet', marker: 'demo-workstation' },
  { path: '/settings', name: 'settings', marker: 'Providers' },
  { path: '/provider/anthropic', name: 'provider', marker: 'dev@example.com' },
] as const;

/**
 * Wait for the app to have the seeded data and to have painted it.
 *
 * A fixed sleep is not a readiness gate. The shell renders before the data
 * arrives, so a sleep that elapses too early measures an empty page -- and
 * an empty page has no overflow, no touch targets and no focus rings, so
 * every assertion in the suite would pass against nothing at all. On a
 * loaded CI runner a 400 ms budget after the first paint is a race, not a
 * guarantee.
 *
 * Two gates, in order:
 *
 *  1. The server has the seed. The demo seeds usage events for four
 *     providers, so the model ranking answers with rows once the seed is
 *     there and the server is up.
 *
 *  2. The page has painted one of the seeded values. The value has to sit
 *     in a leaf that is actually readable: a screen-reader-only helper is a
 *     one-pixel box inside a clipped subtree, and finding the text there
 *     would prove nothing about what the reader sees.
 */
export async function waitForApp(page: Page, marker: string): Promise<void> {
  await page.locator('main').first().waitFor({ state: 'attached' });
  await waitForSeededServer(page);
  await waitForSeededContent(page, marker);
}

/** How long to wait for the seed and for the page to paint it. */
const READY_TIMEOUT = 30_000;

/**
 * Wait until the server answers with the seeded usage rows.
 *
 * This is the half of the gate that does not depend on the page: the demo
 * seeds usage events for four providers, and the model ranking is the one
 * endpoint that carries them.
 */
async function waitForSeededServer(page: Page): Promise<void> {
  await page.waitForFunction(
    async () => {
      try {
        const response = await fetch('/api/v1/usage/top-models');
        if (!response.ok) return false;
        const body = (await response.json()) as { models?: unknown[] };
        return Array.isArray(body.models) && body.models.length > 0;
      } catch {
        return false;
      }
    },
    undefined,
    { timeout: READY_TIMEOUT },
  );
}

/**
 * Wait until the page shows the seeded value in something the reader can
 * actually read.
 *
 * A leaf is readable when it has a box of at least four pixels on a side
 * and is not inside a one-pixel clipped subtree. The latter is the shape
 * of the app's screen-reader-only helpers, and several of them repeat the
 * model names -- the chart's hidden legend, for one -- so a marker found
 * only there would mean the page was still a skeleton.
 */
async function waitForSeededContent(page: Page, marker: string): Promise<void> {
  await page.waitForFunction(
    (text) => {
      const screenReaderOnly = (el: Element): boolean => {
        for (let node = el.parentElement; node; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (
            (style.overflowX === 'hidden' || style.overflowX === 'clip') &&
            node.clientWidth <= 1
          ) {
            return true;
          }
        }
        return false;
      };
      for (const el of document.querySelectorAll('main *')) {
        if (el.children.length > 0) continue; // a leaf holds the text itself
        if (!(el.textContent ?? '').includes(text)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 4 || rect.height < 4) continue;
        if (screenReaderOnly(el)) continue;
        return true;
      }
      return false;
    },
    marker,
    { timeout: READY_TIMEOUT },
  );
}

/** The width of the scrollbar-free viewport the page lays out against. */
export const SLACK = 1;
