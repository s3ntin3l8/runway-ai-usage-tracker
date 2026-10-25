import { expect, test } from '@playwright/test';
import { DEMO_URL, PAGES, PHONE, SLACK, waitForApp } from './support';

// The audit's mobile finding: a long model id pushes the Sessions column
// past the viewport, so the page scrolls sideways on a phone and the reader
// loses the right-hand side of the table.
//
// The check is the PAGE's scroll width, not the position of any one
// element: a strip that scrolls sideways on purpose (the provider's tab
// strip) is not a page that overflows.

for (const { path, name } of PAGES) {
  test(`does not scroll sideways at 390px on ${name}`, async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto(`${DEMO_URL}${path}`);
    await waitForApp(page);

    const measured = await page.evaluate(() => {
      const doc = document.documentElement;
      // An element that overflows the page is only a bug when nothing is
      // scrolling it on purpose — the tab strip is meant to scroll.
      const inScroller = (el: Element): boolean => {
        for (let node = el.parentElement; node; node = node.parentElement) {
          const overflow = getComputedStyle(node).overflowX;
          if (overflow === 'auto' || overflow === 'scroll' || overflow === 'hidden') return true;
        }
        return false;
      };
      const offenders = [...document.querySelectorAll('body *')]
        .filter((el) => {
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.right > doc.clientWidth + 1 && !inScroller(el);
        })
        .slice(0, 5)
        .map((el) => {
          const rect = el.getBoundingClientRect();
          const cls = el.getAttribute('class') ?? '';
          return `${el.tagName.toLowerCase()}${cls ? `.${cls.split(/\s+/).slice(0, 3).join('.')}` : ''} (right edge ${Math.round(rect.right)}px)`;
        });
      return { scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth, offenders };
    });

    expect(
      measured.scrollWidth,
      `the page scrolls sideways; ${measured.offenders.join(', ')}`,
    ).toBeLessThanOrEqual(measured.clientWidth + SLACK);
  });
}
