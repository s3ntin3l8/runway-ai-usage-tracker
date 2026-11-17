import { describe, expect, it } from 'vitest';
import { buildPaths, dependencyGraph, evaluate, ghsaOf } from './audit-build-deps.mjs';

const NOW = new Date('2026-11-17T12:00:00Z');

// vite (build) -> picomatch; vitest (dev only) -> pm2 (dev-only advisory);
// vitest -> picomatch is a SECOND path to the same package.
const lsTree = {
  dependencies: {
    vite: { dependencies: { picomatch: {} } },
    vitest: {
      dependencies: { picomatch: { deduped: true }, 'dev-only-pkg': {} },
    },
  },
};

const advisory = (pkg, url, severity = 'high') => ({
  vulnerabilities: {
    [pkg]: { severity, via: [{ url, severity, title: `${pkg} advisory`, source: 1 }] },
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
    const r = evaluate({ audit: advisory('picomatch', url), lsTree, allowlist: [], now: NOW });
    expect(r.errors).toHaveLength(1);
    expect(r.errors[0]).toContain('vite > picomatch');
  });

  it('only warns for an advisory in dev-only tooling', () => {
    const r = evaluate({ audit: advisory('dev-only-pkg', url), lsTree, allowlist: [], now: NOW });
    expect(r.errors).toEqual([]);
    expect(r.warnings).toHaveLength(1);
  });

  it('ignores low severity (the weekly audit reports those)', () => {
    const r = evaluate({ audit: advisory('picomatch', url, 'low'), lsTree, allowlist: [], now: NOW });
    expect(r).toEqual({ errors: [], warnings: [], notices: [] });
  });

  it('excuses a build-tooling advisory with a live allowlist entry', () => {
    const r = evaluate({ audit: advisory('picomatch', url), lsTree, allowlist: [entry()], now: NOW });
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
    const r = evaluate({ audit: { vulnerabilities: {} }, lsTree, allowlist: [entry()], now: NOW });
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
