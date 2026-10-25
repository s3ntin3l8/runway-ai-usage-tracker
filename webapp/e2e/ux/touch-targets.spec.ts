import { expect, test } from '@playwright/test';
import { DEMO_URL, PAGES, PHONE, waitForApp } from './support';

// The audit's finding (P1-18): "the primary mobile actions were 32–36px
// tall" and the icon-only dismiss buttons were 22px, against the 44px floor
// the design system encodes for phones.
//
// Two rules, because there are two kinds of control:
//
//  1. An ICON-ONLY control -- one whose name comes from an aria-label and
//     that renders no text -- is a target on its own, so it has to be
//     44×44. A switch is excluded: it is operated through its associated
//     <label>, which is the larger target and is part of the control.
//
//  2. A control built from the design system's Button carries the same
//     floor, so its height has to be 44px on a phone.
//
// The design system marks its own buttons with the `ui-button` class (see
// `components/ui/Button.tsx`), so the second rule measures the design
// system's contract rather than a guess at how it happens to be styled. The
// marker sits on the shared `buttonVariants` string, so a link that is
// styled as a button is measured too -- the app has two of those, and they
// are exactly the controls a class-name heuristic would have missed.
//
// Each rule walks EVERY route rather than one route per test, and counts
// the controls it measured. A rule that matched nothing would report no
// findings and pass, which is the failure mode this suite exists to
// prevent: the counts are asserted so a rule that stopped matching fails
// loudly instead of quietly checking nothing.

const TOUCH_FLOOR = 44;

/** The design system's own marker. See `components/ui/Button.tsx`. */
const DESIGN_SYSTEM_BUTTON = 'ui-button';

test('gives every icon-only control a 44px target on a phone', async ({ page }) => {
  // The walk visits every route in one test, so it needs more than the
  // default budget: on a loaded runner each route waits for its own data.
  test.setTimeout(180_000);
  await page.setViewportSize(PHONE);
  const tooSmall: string[] = [];
  let measured = 0;

  for (const spec of PAGES) {
    const { path, name } = spec;
    await page.goto(`${DEMO_URL}${path}`);
    await waitForApp(page, spec);

    const found = await page.evaluate(
      ({ floor, route }) => {
        const small: string[] = [];
        let count = 0;
        for (const el of document.querySelectorAll('button, a[href]')) {
          if (el.getAttribute('role') === 'switch') continue;
          const rect = el.getBoundingClientRect();
          if (rect.width === 0 || rect.height === 0) continue;
          const text = (el.textContent ?? '').trim();
          const label = el.getAttribute('aria-label') ?? '';
          if (text !== '' || label === '') continue; // not an icon-only control
          count += 1;
          if (rect.width < floor || rect.height < floor) {
            small.push(`"${label}" is ${Math.round(rect.width)}×${Math.round(rect.height)}`);
          }
        }
        return { small: small.map((entry) => `${route}: ${entry}`), count };
      },
      { floor: TOUCH_FLOOR, route: name },
    );

    measured += found.count;
    tooSmall.push(...found.small);
  }

  expect(tooSmall, `icon-only controls below the touch floor: ${tooSmall.join(', ')}`).toEqual([]);
  expect(measured, 'the walk found no icon-only controls at all').toBeGreaterThan(0);
});

test('gives the design system\'s actions a 44px height on a phone', async ({ page }) => {
  // Six routes in one test, so the default budget is not enough of one.
  test.setTimeout(180_000);
  await page.setViewportSize(PHONE);
  const tooShort: string[] = [];
  let measured = 0;

  for (const spec of PAGES) {
    const { path, name } = spec;
    await page.goto(`${DEMO_URL}${path}`);
    await waitForApp(page, spec);

    const found = await page.evaluate(
      ({ floor, route, markerClass }) => {
        const short: string[] = [];
        let count = 0;
        for (const el of document.querySelectorAll(`[class~="${markerClass}"]`)) {
          const rect = el.getBoundingClientRect();
          if (rect.width === 0 || rect.height === 0) continue;
          count += 1;
          if (rect.height < floor) {
            const name = (el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 30);
            short.push(`"${name}" is ${Math.round(rect.width)}×${Math.round(rect.height)}`);
          }
        }
        return { short: short.map((entry) => `${route}: ${entry}`), count };
      },
      { floor: TOUCH_FLOOR, route: name, markerClass: DESIGN_SYSTEM_BUTTON },
    );

    measured += found.count;
    tooShort.push(...found.short);
  }

  expect(tooShort, `actions below the touch floor: ${tooShort.join(', ')}`).toEqual([]);
  expect(measured, 'the walk found no design-system buttons at all').toBeGreaterThan(0);
});

test('gives the tab strip a 44px height on a phone', async ({ page }) => {
  // The strip scrolls sideways on a phone, and the triggers are the only way
  // to reach a tab. Six triggers that are 32px tall are six hard taps.
  await page.setViewportSize(PHONE);
  await page.goto(`${DEMO_URL}/provider/anthropic`);
  await waitForApp(page, PAGES.find((p) => p.name === 'provider')!);

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
