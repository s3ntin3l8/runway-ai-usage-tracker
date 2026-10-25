import { spawnSync } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readdir, rename, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

/**
 * Screenshot driver for the reproducible demo pipeline. The orchestrator
 * (scripts/demo/instance.py, via `make screenshots`) seeds a scratch database and
 * serves the built SPA before calling this with SCREENSHOTS_WEB_URL.
 *
 * Profiles (SCREENSHOTS_PROFILE):
 *  - readme (default): assets/screenshots/{page}.png and {page}-light.png for the
 *    README, plus the dark dashboard/history/fleet/settings copied into
 *    webapp/public/screenshots/ for the PWA manifest (sizes in vite.config.ts).
 *  - landing: dark 2x desktop and 3x phone stills for the s3ntin3l8.de landing
 *    page, plus a short loop stepping through a provider's detail tabs
 *    (webm + mp4 + jpg poster; needs ffmpeg). Written to SCREENSHOTS_OUT_DIR.
 *  - audit: phone and tablet viewports of every page, dark and light, written
 *    to docs/audit/assets/<slug>/ (gitignored — the audit *report* is the
 *    committed artifact, the captures stay local). The README profile is
 *    desktop-only, so the mobile layouts had no visual record at all; these
 *    are the reference images for a UI/UX audit. Nothing outside the audit
 *    assets folder is touched.
 */

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const README_DIR = path.join(ROOT, 'assets/screenshots');
const MANIFEST_DIR = path.join(ROOT, 'webapp/public/screenshots');
// The audit assets are local only (see docs/audit/README.md). The slug names
// the audit the captures belong to, so two audits never overwrite each other.
const AUDIT_DIR = path.join(ROOT, 'docs/audit/assets', process.env.SCREENSHOTS_AUDIT_SLUG ?? 'audit');
const WEB_URL = process.env.SCREENSHOTS_WEB_URL ?? 'http://127.0.0.1:3965';
const PROFILE = process.env.SCREENSHOTS_PROFILE ?? 'readme';
const PROVIDER = process.env.SCREENSHOTS_PROVIDER ?? 'anthropic';

// Viewport-sized pages keep the manifest sizes fixed; the provider pages are
// long and are captured full-page for the README only.
const PAGES = [
  { name: 'dashboard', path: '/' },
  { name: 'provider', path: `/provider/${PROVIDER}`, fullPage: true },
  { name: 'provider-activity', path: `/provider/${PROVIDER}`, tab: 'Activity', fullPage: true },
  { name: 'history', path: '/history' },
  { name: 'fleet', path: '/fleet' },
  { name: 'settings', path: '/settings' },
];
const MANIFEST_PAGES = ['dashboard', 'history', 'fleet', 'settings'];
const LANDING_PAGES = ['dashboard', 'provider', 'provider-activity', 'history', 'fleet'];
const LOOP_TABS = ['Activity', 'Sessions', 'Cost', 'Overview'];

async function newContext(browser, theme, options) {
  const context = await browser.newContext({ colorScheme: theme, ...options });
  // useTheme.tsx reads this before first paint.
  await context.addInitScript((t) => globalThis.localStorage.setItem('runway_theme', t), theme);
  return context;
}

// Skeletons (and the chart placeholders) shimmer until their query resolves.
// A busy host can leave them up past networkidle, so wait them out instead of
// shooting a half-loaded page.
async function settled(page, label) {
  try {
    await page.waitForFunction(() => !document.querySelector('.animate-shimmer'), null, {
      timeout: 30_000,
    });
  } catch (err) {
    throw new Error(`skeletons still shimmering after 30s on ${label}`, { cause: err });
  }
}

async function open(page, spec) {
  await page.goto(`${WEB_URL}${spec.path}`, { waitUntil: 'networkidle' });
  if (spec.tab) {
    await page.getByRole('tab', { name: spec.tab }).or(page.getByText(spec.tab, { exact: true })).first().click();
    await page.waitForLoadState('networkidle');
  }
  await settled(page, spec.path);
  // Home banners nag about setup (no alert webhook in a demo); dismiss them the
  // way a user would, so they don't sit on top of every capture.
  for (const close of await page.getByRole('button', { name: 'Dismiss' }).all())
    await close.click().catch(() => {});
  await page.evaluate(() => document.fonts.ready);
  // Let chart entrance animations settle.
  await page.waitForTimeout(800);
}

async function shot(page, dir, name, fullPage) {
  await mkdir(dir, { recursive: true });
  // Write-then-rename so an interrupted run never leaves a half-written file.
  const tmp = path.join(dir, `.${name}`);
  await page.screenshot({ path: tmp, fullPage: Boolean(fullPage) });
  await rename(tmp, path.join(dir, name));
  console.log(`wrote ${name}`);
}

async function captureReadme(browser) {
  for (const theme of ['dark', 'light']) {
    const context = await newContext(browser, theme, { viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    for (const spec of PAGES) {
      await open(page, spec);
      const suffix = theme === 'light' ? '-light' : '';
      await shot(page, README_DIR, `${spec.name}${suffix}.png`, spec.fullPage);
    }
    await context.close();
  }
  await mkdir(MANIFEST_DIR, { recursive: true });
  for (const name of MANIFEST_PAGES)
    await copyFile(path.join(README_DIR, `${name}.png`), path.join(MANIFEST_DIR, `${name}.png`));
}

function ffmpeg(args) {
  const res = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', ...args], { stdio: 'inherit' });
  if (res.error || res.status !== 0) throw new Error(`ffmpeg ${args.join(' ')} failed`);
}

async function recordLoop(browser, outDir) {
  if (spawnSync('ffmpeg', ['-version']).status !== 0) {
    console.warn('ffmpeg not found; skipping the landing loop');
    return;
  }
  // Playwright records in viewport (CSS) pixels regardless of deviceScaleFactor,
  // so the loop is 1x while the stills are 2x; fine for a muted loop.
  const size = { width: 1600, height: 1000 };
  const videoDir = await mkdtemp(path.join(tmpdir(), 'runway-loop-'));
  const context = await newContext(browser, 'dark', { viewport: size, recordVideo: { dir: videoDir, size } });
  const started = Date.now();
  const page = await context.newPage();
  await open(page, { path: `/provider/${PROVIDER}` });
  const offset = (Date.now() - started) / 1000;
  await page.waitForTimeout(1600);
  for (const tab of LOOP_TABS) {
    await page.getByRole('tab', { name: tab }).or(page.getByText(tab, { exact: true })).first().click();
    await page.waitForLoadState('networkidle');
    await settled(page, `loop tab ${tab}`);
    await page.waitForTimeout(1800);
  }
  await context.close();

  const [raw] = await readdir(videoDir);
  const src = path.join(videoDir, raw);
  const base = path.join(outDir, 'provider-tabs');
  const input = ['-ss', offset.toFixed(2), '-i', src, '-an'];
  ffmpeg([...input, '-c:v', 'libvpx-vp9', '-crf', '36', '-b:v', '0', '-row-mt', '1', `${base}.webm`]);
  ffmpeg([...input, '-c:v', 'libx264', '-crf', '26', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', `${base}.mp4`]);
  ffmpeg(['-ss', offset.toFixed(2), '-i', src, '-frames:v', '1', '-q:v', '3', `${base}.jpg`]);
  await rm(videoDir, { recursive: true, force: true });
  console.log('wrote provider-tabs.{webm,mp4,jpg}');
}

async function captureLanding(browser) {
  const outDir = process.env.SCREENSHOTS_OUT_DIR;
  if (!outDir) throw new Error('SCREENSHOTS_OUT_DIR is required for the landing profile');
  const factors = [
    { name: 'desktop', viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 },
    { name: 'mobile', viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true },
  ];
  for (const { name, ...options } of factors) {
    const context = await newContext(browser, 'dark', options);
    const page = await context.newPage();
    for (const spec of PAGES.filter((p) => LANDING_PAGES.includes(p.name))) {
      await open(page, spec);
      await shot(page, outDir, `${spec.name}-${name}.png`, false);
    }
    await context.close();
  }
  await recordLoop(browser, outDir);
}

async function captureAudit(browser) {
  // Viewport-sized, not full-page: a fixed bottom nav is captured at its
  // viewport position, so a full-page shot would not show whether the last
  // row of content clears it.
  const viewports = [
    { name: 'mobile', viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
    { name: 'tablet', viewport: { width: 768, height: 1024 }, deviceScaleFactor: 2 },
  ];
  for (const theme of ['dark', 'light']) {
    for (const { name, ...options } of viewports) {
      const context = await newContext(browser, theme, options);
      const page = await context.newPage();
      for (const spec of PAGES) {
        await open(page, spec);
        const suffix = theme === 'light' ? '-light' : '';
        await shot(page, AUDIT_DIR, `${spec.name}-${name}${suffix}.png`, false);
      }
      await context.close();
    }
  }
}

if (!['readme', 'landing', 'audit'].includes(PROFILE))
  throw new Error(`unknown SCREENSHOTS_PROFILE "${PROFILE}" (readme | landing | audit)`);
const browser = await chromium.launch();
try {
  if (PROFILE === 'landing') await captureLanding(browser);
  else if (PROFILE === 'audit') await captureAudit(browser);
  else await captureReadme(browser);
} finally {
  await browser.close();
}
