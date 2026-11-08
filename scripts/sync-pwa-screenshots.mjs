/**
 * Copies the screenshots `webapp/pwa-screenshots.json` lists (the install-dialog previews in the PWA
 * manifest, see `webapp/vite.config.ts`) from docs/screenshots/, where `make screenshots` renders both
 * colour schemes, into webapp/public/screenshots/, so the installed app serves them from its own origin.
 * The manifest has no per-scheme screenshots, so it names the dark set. Anything else in the destination is
 * removed: the folder is only ever a copy.
 *
 * Usage: node scripts/sync-pwa-screenshots.mjs   (run by `make screenshots`)
 */
import { copyFile, mkdir, readFile, readdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const from = path.join(root, 'docs', 'screenshots');
const to = path.join(root, 'webapp', 'public', 'screenshots');

const listed = JSON.parse(await readFile(path.join(root, 'webapp', 'pwa-screenshots.json'), 'utf8'));
const wanted = new Set(listed.map((s) => path.basename(s.src)));

await mkdir(to, { recursive: true });
for (const file of await readdir(to))
  if (!wanted.has(file)) await rm(path.join(to, file), { recursive: true, force: true });
for (const file of wanted) {
  await copyFile(path.join(from, file), path.join(to, file)).catch((err) => {
    throw new Error(
      `${file} is in webapp/pwa-screenshots.json but was not rendered into docs/screenshots: ${err.message}`,
    );
  });
}
console.log(`[screenshots] copied ${wanted.size} images into webapp/public/screenshots`);
