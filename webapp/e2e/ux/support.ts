// Shared helpers for the UX suite.
//
// The suite runs against the demo instance (see `playwright.ux.config.ts`),
// which serves the built SPA on one port with seeded data.

import type { Page } from '@playwright/test';

export const DEMO_URL = 'http://127.0.0.1:3965';

/** A phone. The audit's mobile findings are all about this width. */
export const PHONE = { width: 390, height: 844 };

/**
 * A route the suite visits, and the seeded value it has to show before it is
 * measured.
 */
export interface PageSpec {
  /** The route the suite opens. */
  path: string;
  /** A short name for the route, used in the test names. */
  name: string;
  /**
   * The seeded value the page has to show before it is measured: a
   * provider's display name, a model id, the seeded account's address, a
   * sidecar's name. None of them is a number that moves with the date, and
   * none of them is derived from a count the seed happens to produce — a
   * count changes the moment the seed grows a model, and a marker that
   * changes with it is a marker that times out on a later run.
   */
  marker: string;
  /**
   * Set when the marker can only exist in a chart's text alternative.
   *
   * The rankings are drawn into a canvas, so their labels are pixels and a
   * screen-reader list beside the chart — never text a reader can point at.
   * That list is built from the rows the chart draws, so it exists only once
   * the ranking has arrived, which is what the marker is there to prove.
   */
  inChartText?: boolean;
}

/**
 * The routes that carry content on a phone, and the seeded value each one
 * has to show before it is measured.
 *
 * `/settings` is a list of sections on a phone and one section on a
 * desktop, so both are worth a look; the provider page is the densest
 * surface the app has.
 *
 * The marker is what makes the suite honest, and every marker is a value
 * the seed produces. The settings page has no usage data of its own -- its
 * content IS the section list, and the first section in it is the thing the
 * audit found missing.
 */
export const PAGES: readonly PageSpec[] = [
  { path: '/', name: 'home', marker: 'ChatGPT' },
  {
    path: '/insights',
    name: 'insights',
    // A seeded model id, not the ranking's title ("Top 8 models"): the
    // title states a count, and the count changes whenever the seed's model
    // set does.
    marker: 'gpt-5.5',
    inChartText: true,
  },
  { path: '/history', name: 'history', marker: 'dev@example.com' },
  { path: '/fleet', name: 'fleet', marker: 'demo-workstation' },
  { path: '/settings', name: 'settings', marker: 'Providers' },
  { path: '/provider/anthropic', name: 'provider', marker: 'dev@example.com' },
];

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
export async function waitForApp(page: Page, spec: PageSpec): Promise<void> {
  await page.locator('main').first().waitFor({ state: 'attached' });
  await waitForSeededServer(page);
  await waitForSeededContent(page, spec);
  await waitForSettledPage(page);
}

/** How long to wait for the seed and for the page to paint it. */
const READY_TIMEOUT = 30_000;

/**
 * Wait until the page has finished arriving.
 *
 * The marker above proves that one value reached the screen. It says nothing
 * about the cards around it: each fetches its own data, and a page measured
 * while they are still arriving is a page measured against a half-drawn
 * layout. That is not hypothetical — in CI the bottom-nav check scrolled to a
 * height the page had not reached yet, and measured content that had not
 * been drawn yet.
 *
 * Two gates. The page's own requests have to be done, or a card is still on
 * its way. And the layout has to stop changing, because a card that lands
 * after the scroll has already happened moves everything below it.
 */
async function waitForSettledPage(page: Page): Promise<void> {
  // The app polls every couple of minutes, so there is always a quiet
  // window between polls. A timeout here is not fatal: the stability gate
  // below is the one that decides.
  await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => undefined);
  await page.waitForFunction(
    () => {
      const main = document.querySelector('main');
      const doc = document.documentElement;
      const key = [
        doc.scrollHeight,
        doc.scrollWidth,
        main ? Math.round(main.getBoundingClientRect().height) : 0,
      ].join(':');
      const store = window as unknown as { uxLayoutKey?: string; uxLayoutHits?: number };
      if (store.uxLayoutKey === key) {
        store.uxLayoutHits = (store.uxLayoutHits ?? 0) + 1;
      } else {
        store.uxLayoutKey = key;
        store.uxLayoutHits = 0;
      }
      // The same layout for several frames in a row, so a card that is on
      // its way does not slip in between the check and the measurement.
      return (store.uxLayoutHits ?? 0) >= 10;
    },
    undefined,
    { timeout: READY_TIMEOUT },
  );
}

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
 *
 * A route that says its marker lives in a chart's text alternative is
 * checked differently: the rankings are drawn into a canvas, so their
 * labels are pixels and a screen-reader list beside the chart, never text a
 * reader can point at. That list is built from the rows the chart draws, so
 * it exists only once the ranking has arrived -- the same thing the
 * readable rule proves for the other routes.
 */
async function waitForSeededContent(page: Page, spec: PageSpec): Promise<void> {
  await page.waitForFunction(
    ({ text, inChartText }) => {
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
        if (!(el.textContent ?? '').includes(text)) continue;
        const hidden = screenReaderOnly(el);
        // The chart's text alternative: a list built from the rows the chart
        // draws, so it exists only once the ranking has arrived.
        if (inChartText && hidden) return true;
        if (el.children.length > 0) continue; // a leaf holds the text itself
        const rect = el.getBoundingClientRect();
        if (rect.width < 4 || rect.height < 4) continue;
        if (hidden) continue;
        return true;
      }
      return false;
    },
    { text: spec.marker, inChartText: spec.inChartText ?? false },
    { timeout: READY_TIMEOUT },
  );
}

/** The width of the scrollbar-free viewport the page lays out against. */
export const SLACK = 1;
