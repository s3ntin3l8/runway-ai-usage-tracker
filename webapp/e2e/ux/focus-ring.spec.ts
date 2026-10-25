import { expect, test } from '@playwright/test';
import { DEMO_URL, PHONE, waitForApp } from './support';

// The audit's finding: `ui/Tabs.tsx` applied `outline-none` to a focusable
// element, which suppressed the global focus ring. A control that is focused
// and shows nothing is a control a keyboard user cannot find.
//
// Two things are checked for every focused control:
//
//  1. It shows a focus indicator at all — an outline or a ring.
//  2. The indicator is painted INSIDE the viewport. A ring on a control
//     half off-screen is clipped, so the reader sees the control and not the
//     indication that it has focus.

test('every control the keyboard reaches shows its focus ring inside the viewport', async ({ page }) => {
  await page.setViewportSize(PHONE);
  await page.goto(`${DEMO_URL}/provider/anthropic`);
  await waitForApp(page);

  // The provider page has the densest strip of controls on the app: the
  // header actions, the tab strip (which scrolls sideways) and the bottom
  // nav. Sixteen stops is enough to walk all of them.
  const seen: string[] = [];
  for (let step = 0; step < 16; step += 1) {
    await page.keyboard.press('Tab');
    const focused = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      if (!el || el === document.body) return null;
      const style = getComputedStyle(el);
      const outline = style.outlineStyle !== 'none' && parseFloat(style.outlineWidth || '0') > 0;
      const ring = style.boxShadow !== 'none' && style.boxShadow !== '';
      const rect = el.getBoundingClientRect();
      return {
        name:
          el.getAttribute('aria-label') ??
          (el.textContent ?? '').trim().slice(0, 30) ??
          el.tagName,
        tag: el.tagName.toLowerCase(),
        outline,
        ring,
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
      };
    });
    if (!focused) continue;
    if (focused.width === 0 || focused.height === 0) continue;
    seen.push(`${focused.tag} "${focused.name}"`);

    expect(
      focused.outline || focused.ring,
      `"${focused.name}" is focused but paints no focus indicator`,
    ).toBe(true);
    // The ring is painted outside the control's box, so a control that sits
    // against the left or right edge of the screen has its ring clipped off.
    expect(
      focused.left,
      `"${focused.name}" is focused but its left edge is off-screen`,
    ).toBeGreaterThanOrEqual(0);
    expect(
      focused.right,
      `"${focused.name}" is focused but its right edge is off-screen`,
    ).toBeLessThanOrEqual(focused.viewportWidth);
    // Vertically the control only has to be ON screen: the tab panel is
    // focusable and taller than the viewport, and its ring is painted at the
    // top of it, which is on screen. What must not happen is a focused
    // control that is nowhere near the viewport.
    expect(
      focused.top,
      `"${focused.name}" is focused but it is below the screen`,
    ).toBeLessThan(focused.viewportHeight);
    expect(
      focused.bottom,
      `"${focused.name}" is focused but it is above the screen`,
    ).toBeGreaterThan(0);
  }

  // The walk has to have reached something, or the assertions above proved
  // nothing at all.
  expect(seen.length, 'the keyboard walk reached no controls').toBeGreaterThan(3);
});

test('the deep-linked tab is brought into view instead of sitting off-screen', async ({ page }) => {
  // The provider's tab strip scrolls sideways on a phone and the last tabs
  // are past the fold. A deep link has to bring the tab it names into view,
  // or the reader lands on a page that looks like the wrong tab is selected.
  await page.setViewportSize(PHONE);
  await page.goto(`${DEMO_URL}/provider/anthropic?tab=cost`);
  await waitForApp(page);
  await page.waitForTimeout(500);

  const active = page.locator('[role="tab"][data-state="active"]');
  await expect(active).toBeVisible();
  const box = await active.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x, 'the active tab is off the left of the screen').toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width, 'the active tab is off the right of the screen').toBeLessThanOrEqual(
    PHONE.width,
  );
});
