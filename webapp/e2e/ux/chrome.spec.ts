import { expect, test } from '@playwright/test';
import { DEMO_URL, PAGES, PHONE, waitForApp } from './support';

// The audit's finding (P1-16): the bottom nav is h-16 PLUS the home
// indicator inset on a notched iPhone (≈98px), but the page reserved a
// fixed pb-20 (80px) — so the last ~18px of every page sat UNDER the nav.
//
// A desktop browser has no home indicator, so the inset is zero here and the
// nav is 65px. The check is the relationship the fix encodes: the page has
// to reserve room for the nav it actually has. On a device with an inset
// the reserved padding grows with it, and the same relationship holds.

test('the page reserves room for the fixed bottom nav', async ({ page }) => {
  await page.setViewportSize(PHONE);
  await page.goto(`${DEMO_URL}/insights`);
  await waitForApp(page, PAGES.find((p) => p.name === 'insights')!);

  const measured = await page.evaluate(() => {
    const nav = document.querySelector('nav.fixed');
    const main = document.querySelector('main');
    if (!nav || !main) return null;
    const navRect = nav.getBoundingClientRect();
    return {
      navHeight: Math.round(navRect.height),
      reserved: Math.round(parseFloat(getComputedStyle(main).paddingBottom) || 0),
    };
  });

  expect(measured, 'the bottom nav or the main content is missing').not.toBeNull();
  expect(
    measured!.reserved,
    `the page reserves ${measured!.reserved}px for a ${measured!.navHeight}px bottom nav`,
  ).toBeGreaterThanOrEqual(measured!.navHeight);
});

for (const spec of PAGES) {
  const { path, name } = spec;
  test(`the fixed chrome does not cover the content on ${name}`, async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto(`${DEMO_URL}${path}`);
    await waitForApp(page, spec);

    // Scroll to the very bottom: that is where the last of the content sits,
    // and where the nav is most likely to cover it. The scroll is repeated
    // until the page stops growing, so a card that lands after the first
    // scroll cannot leave the page short of its bottom.
    await page.evaluate(async () => {
      const bottom = () => window.scrollTo(0, document.documentElement.scrollHeight);
      for (let attempt = 0; attempt < 10; attempt += 1) {
        bottom();
        const height = document.documentElement.scrollHeight;
        await new Promise((resolve) => setTimeout(resolve, 100));
        bottom();
        if (document.documentElement.scrollHeight === height) break;
      }
    });

    const covered = await page.evaluate(() => {
      const nav = document.querySelector('nav.fixed');
      const main = document.querySelector('main');
      if (!nav || !main) return [];
      const navRect = nav.getBoundingClientRect();
      // Content inside a clipping ancestor is not on screen at all, so the
      // nav cannot be covering it.
      const clipped = (el: Element): boolean => {
        for (let node = el.parentElement; node; node = node.parentElement) {
          if (getComputedStyle(node).overflow !== 'visible') return true;
        }
        return false;
      };
      const hits: string[] = [];
      for (const el of main.querySelectorAll('*')) {
        if (el.children.length > 0) continue; // a leaf: the content itself
        if ((el.textContent ?? '').trim() === '') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        // Only content that is actually on screen can be covered by the nav.
        // This comes before the clipping walk, which is the expensive part:
        // a page with charts has thousands of leaves.
        if (rect.bottom < navRect.top || rect.top > navRect.bottom) continue;
        if (rect.bottom <= navRect.top + 1) continue;
        if (clipped(el)) continue;
        hits.push(`"${(el.textContent ?? '').trim().slice(0, 30)}"`);
      }
      return hits.slice(0, 5);
    });

    expect(covered, `the bottom nav covers the content: ${covered.join(', ')}`).toEqual([]);
  });
}
