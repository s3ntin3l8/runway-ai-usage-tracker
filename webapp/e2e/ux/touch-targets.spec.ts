import { expect, test } from '@playwright/test';
import { DEMO_URL, PAGES, PHONE, waitForApp } from './support';

// The audit's finding (P1-18): "the primary mobile actions were 32–36px
// tall" and the icon-only dismiss buttons were 22px, against the 44px floor
// the design system encodes for phones.
//
// Two rules, because there are two kinds of control:
//
//  1. An ICON-ONLY button — one whose name comes from an aria-label and that
//     renders no text — is a target on its own, so it has to be 44×44. A
//     switch is excluded: it is operated through its associated <label>,
//     which is the larger target and is part of the control.
//
//  2. A control built from the design system's Button carries the same
//     floor, so its height has to be 44px on a phone.

const TOUCH_FLOOR = 44;

for (const { path, name } of PAGES) {
  test(`gives every icon-only control a ${TOUCH_FLOOR}px target on ${name}`, async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto(`${DEMO_URL}${path}`);
    await waitForApp(page);

    const tooSmall = await page.evaluate((floor) => {
      const small: string[] = [];
      for (const el of document.querySelectorAll('button')) {
        if (el.getAttribute('role') === 'switch') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        const text = (el.textContent ?? '').trim();
        const label = el.getAttribute('aria-label') ?? '';
        if (text !== '' || label === '') continue; // not an icon-only control
        if (rect.width < floor || rect.height < floor) {
          small.push(`"${label}" is ${Math.round(rect.width)}×${Math.round(rect.height)}`);
        }
      }
      return small;
    }, TOUCH_FLOOR);

    expect(tooSmall, `icon-only controls below the touch floor: ${tooSmall.join(', ')}`).toEqual([]);
  });
}

test('gives the primary actions a 44px height on a phone', async ({ page }) => {
  await page.setViewportSize(PHONE);
  await page.goto(`${DEMO_URL}/provider/anthropic`);
  await waitForApp(page);

  const tooShort = await page.evaluate((floor) => {
    const short: string[] = [];
    for (const el of document.querySelectorAll('button')) {
      // The design system's own signature: a centred inline-flex row.
      if (!el.classList.contains('inline-flex')) continue;
      if (!el.classList.contains('justify-center')) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue;
      if (rect.height < floor) {
        const name = (el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 30);
        short.push(`"${name}" is ${Math.round(rect.width)}×${Math.round(rect.height)}`);
      }
    }
    return short;
  }, TOUCH_FLOOR);

  expect(tooShort, `actions below the touch floor: ${tooShort.join(', ')}`).toEqual([]);
});

test('gives the tab strip a 44px height on a phone', async ({ page }) => {
  // The strip scrolls sideways on a phone, and the triggers are the only way
  // to reach a tab. Six triggers that are 32px tall are six hard taps.
  await page.setViewportSize(PHONE);
  await page.goto(`${DEMO_URL}/provider/anthropic`);
  await waitForApp(page);

  const triggers = page.locator('[role="tab"]');
  const count = await triggers.count();
  expect(count).toBeGreaterThan(0);
  for (let i = 0; i < count; i += 1) {
    const box = await triggers.nth(i).boundingBox();
    const name = (await triggers.nth(i).textContent())?.trim() ?? '';
    expect(box, `"${name}" has no box`).not.toBeNull();
    expect(box!.height, `the "${name}" tab is ${box!.height}px tall`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
});
