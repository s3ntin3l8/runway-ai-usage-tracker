import { expect, test } from '@playwright/test';
import { DEMO_URL, PAGES, PHONE, SLACK, waitForApp } from './support';

// The audit's mobile finding: a long model id pushes the Sessions column
// past the viewport, so the page scrolls sideways on a phone and the reader
// loses the right-hand side of the table.
//
// There are two ways to lose the right-hand side, and the suite checks
// both:
//
//  1. The PAGE scrolls sideways. The wide element is not being scrolled on
//     purpose, so the whole page moves with it.
//
//  2. The content is CLIPPED. `overflow: hidden` does not scroll, it cuts:
//     whatever is past the edge is gone, with no scrollbar to reach it and
//     no ellipsis to say that something is missing. That is the audit's
//     symptom in its purest form, so a clipping ancestor is not an excuse
//     for a wide element -- it IS the finding.
//
// A deliberate horizontal SCROLLER (the provider's tab strip) is exempt:
// the reader can reach the content by scrolling it.

for (const spec of PAGES) {
  const { path, name } = spec;
  test(`does not scroll sideways at 390px on ${name}`, async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto(`${DEMO_URL}${path}`);
    await waitForApp(page, spec);

    const measured = await page.evaluate(() => {
      const doc = document.documentElement;
      const overflowX = (el: Element): string => getComputedStyle(el).overflowX;

      // An element that overflows the page is only a bug when nothing is
      // scrolling it on purpose. A clipping ancestor is not a scroller, but
      // it does mean the element is not what makes the PAGE scroll -- the
      // clipping rule below reports that case instead.
      const intercepted = (el: Element): boolean => {
        for (let node = el.parentElement; node; node = node.parentElement) {
          const overflow = overflowX(node);
          if (overflow === 'auto' || overflow === 'scroll') return true;
          if (overflow === 'hidden' || overflow === 'clip') return true;
        }
        return false;
      };
      const offenders = [...document.querySelectorAll('body *')]
        .filter((el) => {
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.right > doc.clientWidth + 1 && !intercepted(el);
        })
        .slice(0, 5)
        .map((el) => {
          const rect = el.getBoundingClientRect();
          const cls = el.getAttribute('class') ?? '';
          return `${el.tagName.toLowerCase()}${cls ? `.${cls.split(/\s+/).slice(0, 3).join('.')}` : ''} (right edge ${Math.round(rect.right)}px)`;
        });

      // The clipping rule. An element whose content is wider than its own
      // box, and whose box cuts it off, loses that content. Two shapes are
      // not findings:
      //
      //  - a one-pixel box: that is a screen-reader-only helper, and there
      //    is nothing a sighted reader can lose.
      //  - an ellipsis: the text is deliberately truncated and the reader
      //    is told that there is more.
      const clipped: string[] = [];
      let inspected = 0;
      for (const el of document.querySelectorAll('body *')) {
        const style = getComputedStyle(el);
        if (style.overflowX !== 'hidden' && style.overflowX !== 'clip') continue;
        inspected += 1;
        if (el.clientWidth <= 1) continue;
        if (style.textOverflow === 'ellipsis') continue;
        if (el.scrollWidth <= el.clientWidth + 1) continue;
        const cls = el.getAttribute('class') ?? '';
        clipped.push(
          `${el.tagName.toLowerCase()}${cls ? `.${cls.split(/\s+/).slice(0, 3).join('.')}` : ''} cuts ${el.scrollWidth - el.clientWidth}px away`,
        );
      }

      return {
        scrollWidth: doc.scrollWidth,
        clientWidth: doc.clientWidth,
        offenders,
        inspected,
        clipped: clipped.slice(0, 5),
      };
    });

    expect(
      measured.scrollWidth,
      `the page scrolls sideways; ${measured.offenders.join(', ')}`,
    ).toBeLessThanOrEqual(measured.clientWidth + SLACK);
    expect(
      measured.clipped,
      `content is cut off with nothing to scroll it back; ${measured.clipped.join(', ')}`,
    ).toEqual([]);
    // The rule has to have looked at something. The app clips a lot on
    // purpose -- the truncated model names, the screen-reader-only chart
    // descriptions -- and those are exempted, but the rule still has to
    // have seen them for the exemption to mean anything.
    expect(measured.inspected, 'the page has no clipping containers at all').toBeGreaterThan(0);
  });
}
