/// <reference types="node" />
// Locale guard: a timestamp may only be formatted through lib/tz.ts.
//
// A raw `toLocaleString()` renders in the browser's locale AND its timezone.
// The dashboard resolves ONE timezone for every user (user_timezone →
// env_timezone → browser → UTC) and formats every instant through lib/tz.ts,
// so the same moment reads the same wherever the browser happens to be. A
// raw call silently opts out of that — which is how three dialogs ended up
// showing a different local time than the rest of the app.
//
// The source is read from disk rather than imported: the point is to catch
// the *call site*, not the rendered output.

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

function findSrcDir(): string {
  // vitest may run from the package directory or from the repository root
  // (CI runs it with webapp/ as the working directory, but a developer may
  // not), so look for the source tree under both layouts.
  let dir = process.cwd();
  for (let i = 0; i < 6; i += 1) {
    for (const rel of ['src', 'webapp/src']) {
      const candidate = resolve(dir, rel);
      if (existsSync(candidate)) return candidate;
    }
    dir = resolve(dir, '..');
  }
  throw new Error('Could not find the webapp src directory');
}

const SRC = findSrcDir();

// lib/tz.ts is where a date may be formatted. lib/format.ts builds its
// formatters on Intl.NumberFormat with a pinned locale, so it has no
// locale-dependent call to make. test/setup.ts pins the locale in the test
// environment itself.
const ALLOWED_FILES = new Set(['lib/tz.ts', 'lib/format.ts', 'test/setup.ts']);

// The one sanctioned exception: a pinned YYYY-MM-DD key for a fingerprint,
// not a value a user reads. It names its locale explicitly.
const SANCTIONED = new Map<string, RegExp>([
  ['features/home/Banners.tsx', /toLocaleDateString\('en-CA'\)/],
]);

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      sourceFiles(full, out);
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.test\.(ts|tsx)$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

describe('locale guard', () => {
  it('formats no timestamp outside lib/tz.ts', () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      const rel = relative(SRC, file).split('\\').join('/');
      if (ALLOWED_FILES.has(rel)) continue;
      const lines = readFileSync(file, 'utf8').split('\n');
      lines.forEach((line, index) => {
        if (!/toLocale(String|DateString|TimeString)\s*\(/.test(line)) return;
        if (SANCTIONED.get(rel)?.test(line)) return;
        offenders.push(`${rel}:${index + 1}: ${line.trim()}`);
      });
    }
    expect(offenders).toEqual([]);
  });
});
