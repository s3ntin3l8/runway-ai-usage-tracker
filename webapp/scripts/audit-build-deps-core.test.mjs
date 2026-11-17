import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  BUILD_ROOTS,
  buildPaths,
  dependencyGraph,
  escapeWorkflowCommand,
  evaluate,
  ghsaOf,
  missingRoots,
} from './audit-build-deps-core.mjs';

const NOW = new Date('2026-11-17T12:00:00Z');

// vite (build) -> picomatch; vitest (dev only) -> pm2 (dev-only advisory);
// vitest -> picomatch is a SECOND path to the same package.
const lsTree = {
  dependencies: {
    // Every build root must be installed (a missing one is itself an error); only
    // vite reaches anything in this fixture.
    ...Object.fromEntries(BUILD_ROOTS.map((root) => [root, {}])),
    vite: { dependencies: { picomatch: {} } },
    vitest: {
      dependencies: { picomatch: { deduped: true }, 'dev-only-pkg': {} },
    },
  },
};

const advisory = (pkg, url, severity = 'high') => ({
  vulnerabilities: {
    [pkg]: {
      severity,
      via: [{ url, severity, title: `${pkg} advisory`, source: 1 }],
    },
  },
});

const entry = (over = {}) => ({
  id: 'GHSA-aaaa-bbbb-cccc',
  package: 'picomatch',
  reason: 'no patched release',
  issue: '#1',
  expires: '2027-02-15',
  ...over,
});

describe('ghsaOf', () => {
  it('pulls the id out of an advisory URL', () => {
    expect(ghsaOf('https://github.com/advisories/GHSA-fc5q-39xc-vjpf')).toBe('GHSA-fc5q-39xc-vjpf');
    expect(ghsaOf(undefined)).toBeNull();
  });
});

describe('buildPaths', () => {
  it('reaches a dependency shared with dev tooling through the build root', () => {
    const paths = buildPaths(dependencyGraph(lsTree));
    expect(paths.get('picomatch')).toEqual(['vite', 'picomatch']);
    expect(paths.has('dev-only-pkg')).toBe(false);
  });
});

describe('evaluate', () => {
  const url = 'https://github.com/advisories/GHSA-aaaa-bbbb-cccc';

  it('blocks a moderate+ advisory in build tooling and names the path', () => {
    const r = evaluate({
      audit: advisory('picomatch', url),
      lsTree,
      allowlist: [],
      now: NOW,
    });
    expect(r.errors).toHaveLength(1);
    expect(r.errors[0]).toContain('vite > picomatch');
  });

  it('only warns for an advisory in dev-only tooling', () => {
    const r = evaluate({
      audit: advisory('dev-only-pkg', url),
      lsTree,
      allowlist: [],
      now: NOW,
    });
    expect(r.errors).toEqual([]);
    expect(r.warnings).toHaveLength(1);
  });

  it('ignores low severity (the weekly audit reports those)', () => {
    const r = evaluate({
      audit: advisory('picomatch', url, 'low'),
      lsTree,
      allowlist: [],
      now: NOW,
    });
    expect(r).toEqual({ errors: [], warnings: [], notices: [] });
  });

  it('excuses a build-tooling advisory with a live allowlist entry', () => {
    const r = evaluate({
      audit: advisory('picomatch', url),
      lsTree,
      allowlist: [entry()],
      now: NOW,
    });
    expect(r.errors).toEqual([]);
    expect(r.notices[0]).toContain('excused until 2027-02-15');
  });

  it('fails an expired entry once, without calling it excused', () => {
    const r = evaluate({
      audit: advisory('picomatch', url),
      lsTree,
      allowlist: [entry({ expires: '2026-11-16' })],
      now: NOW,
    });
    expect(r.errors).toHaveLength(1);
    expect(r.errors[0]).toContain('expired on 2026-11-16');
    expect(r.notices).toEqual([]);
  });

  it('fails an entry with no valid expiry', () => {
    const r = evaluate({
      audit: advisory('picomatch', url),
      lsTree,
      allowlist: [entry({ expires: undefined })],
      now: NOW,
    });
    expect(r.errors[0]).toContain('no valid "expires"');
  });

  it('tells you to remove an entry that matches nothing', () => {
    const r = evaluate({
      audit: { vulnerabilities: {} },
      lsTree,
      allowlist: [entry()],
      now: NOW,
    });
    expect(r.errors).toEqual([]);
    expect(r.notices[0]).toContain('remove the entry');
  });

  it('does not let an entry for one package excuse another', () => {
    const r = evaluate({
      audit: advisory('picomatch', url),
      lsTree,
      allowlist: [entry({ package: 'other' })],
      now: NOW,
    });
    expect(r.errors).toHaveLength(1);
  });
});

describe('npm audit meta entries', () => {
  it('ignores string `via` entries (a chain link) and reads only the root advisory', () => {
    // 18 of the 19 findings on the real tree look like `vite`/`tinyglobby`: their
    // `via` is a package name, and only `picomatch` carries the advisory object.
    const audit = {
      vulnerabilities: {
        vite: { severity: 'high', via: ['picomatch'] },
        picomatch: {
          severity: 'high',
          via: [
            {
              url: 'https://github.com/advisories/GHSA-aaaa-bbbb-cccc',
              severity: 'high',
              title: 'ReDoS',
              source: 1,
            },
          ],
        },
      },
    };
    const r = evaluate({ audit, lsTree, allowlist: [entry()], now: NOW });
    // One advisory, excused; no bogus "unknown" finding for the string entries.
    expect(r.errors).toEqual([]);
    expect(r.notices).toHaveLength(1);
    expect(JSON.stringify(r)).not.toContain('unknown');
  });
});

describe('build roots', () => {
  it('reports a root the install does not have, as a blocking error', () => {
    const tree = {
      dependencies: { vite: { dependencies: { picomatch: {} } } },
    };
    expect(missingRoots(dependencyGraph(tree))).toContain('@vitejs/plugin-react');
    const r = evaluate({
      audit: { vulnerabilities: {} },
      lsTree: tree,
      allowlist: [],
      now: NOW,
    });
    expect(r.errors.some((e) => e.includes('"@vitejs/plugin-react" but it is not installed'))).toBe(
      true,
    );
  });

  it('names every package the build actually runs (configs and script binaries)', () => {
    // The roots are a hand-kept list, so pin them to what the build executes: a
    // plugin added to vite.config.ts or a new binary in a build script must be a root,
    // or its subtree would quietly fall to the warn tier.
    const webapp = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
    const pkgOf = (spec) =>
      (spec.startsWith('@') ? spec.split('/').slice(0, 2) : spec.split('/').slice(0, 1)).join('/');
    const imported = new Set();
    for (const config of ['vite.config.ts', 'pwa-assets.config.ts']) {
      const src = readFileSync(path.join(webapp, config), 'utf8');
      for (const m of src.matchAll(/from\s+['"]([^'".][^'"]*)['"]/g)) {
        if (!m[1].startsWith('node:')) imported.add(pkgOf(m[1]));
      }
    }
    expect([...imported].filter((p) => !BUILD_ROOTS.includes(p))).toEqual([]);

    // Binaries named in the `build` and `generate-pwa-assets` scripts, mapped to
    // the installed package that provides each through its `bin` field.
    const scripts = JSON.parse(readFileSync(path.join(webapp, 'package.json'), 'utf8')).scripts;
    const used = new Set(
      [scripts.build, scripts['generate-pwa-assets']]
        .join(' && ')
        .split(/&&|\|\|/)
        .map((c) => c.trim().split(/\s+/)[0])
        .filter(Boolean),
    );
    const providers = new Map();
    const nm = path.join(webapp, 'node_modules');
    const pkgDirs = readdirSync(nm).flatMap((d) =>
      d.startsWith('@') ? readdirSync(path.join(nm, d)).map((n) => `${d}/${n}`) : [d],
    );
    for (const dir of pkgDirs) {
      let pkg;
      try {
        pkg = JSON.parse(readFileSync(path.join(nm, dir, 'package.json'), 'utf8'));
      } catch {
        continue;
      }
      const bins = typeof pkg.bin === 'string' ? [pkg.name] : Object.keys(pkg.bin ?? {});
      for (const b of bins) if (used.has(b) && !providers.has(b)) providers.set(b, pkg.name);
    }
    for (const bin of used) {
      expect(providers.has(bin), `no installed package provides the \`${bin}\` binary`).toBe(true);
      expect(BUILD_ROOTS, `\`${bin}\` comes from ${providers.get(bin)}`).toContain(
        providers.get(bin),
      );
    }
  });
});

describe('allowlist validation', () => {
  it.each(['id', 'package', 'reason', 'issue'])('errors on an entry with no %s', (field) => {
    const r = evaluate({
      audit: { vulnerabilities: {} },
      lsTree,
      allowlist: [entry({ [field]: undefined })],
      now: NOW,
    });
    expect(r.errors.some((e) => e.includes(`has no "${field}"`))).toBe(true);
  });
});

describe('escapeWorkflowCommand', () => {
  it('neutralises %, CR and LF so registry text cannot start another command', () => {
    expect(escapeWorkflowCommand('a\n::error::boom\r100%')).toBe('a%0A::error::boom%0D100%25');
  });
});
