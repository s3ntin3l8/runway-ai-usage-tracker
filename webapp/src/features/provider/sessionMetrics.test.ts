import type { SessionEntry } from '@/api/types';
import { bucketCost, sessionCachePct, sessionCost, sessionTokens } from './sessionMetrics';

// Minimal session shape; spread overrides per case.
const s = (o: Partial<SessionEntry> = {}): SessionEntry => ({ session_id: 'abc', ...o });

describe('sessionTokens', () => {
  it('uses tokens_total when present', () => {
    expect(sessionTokens(s({ tokens_total: 1000 }))).toBe(1000);
  });

  it('falls back to summing by_model totals when tokens_total is absent', () => {
    const sess = s({ by_model: [{ tokens_total: 300 }, { tokens_total: 200 }] as never });
    expect(sessionTokens(sess)).toBe(500);
  });

  it('subtracts cache from the total source when excludeCache is set', () => {
    const sess = s({ tokens_total: 1000, tokens_cache_read: 600, tokens_cache_create: 100 });
    expect(sessionTokens(sess, true)).toBe(300);
  });

  it('subtracts cache from the by_model source when tokens_total is absent', () => {
    const sess = s({
      by_model: [
        { tokens_total: 400, tokens_cache_read: 100, tokens_cache_create: 50 },
        { tokens_total: 200, tokens_cache_read: 20, tokens_cache_create: 0 },
      ] as never,
    });
    // total 600 − cache (170) = 430
    expect(sessionTokens(sess, true)).toBe(430);
  });

  it('treats missing fields as zero', () => {
    expect(sessionTokens(s())).toBe(0);
    expect(sessionTokens(s(), true)).toBe(0);
  });
});

describe('sessionCost', () => {
  it('uses cost_usd when present', () => {
    expect(sessionCost(s({ cost_usd: 2.5 }))).toBe(2.5);
  });

  it('falls back to summing by_model costs', () => {
    const sess = s({ by_model: [{ cost_usd: 1.5 }, { cost_usd: 0.25 }] as never });
    expect(sessionCost(sess)).toBe(1.75);
  });

  it('is zero when nothing is available', () => {
    expect(sessionCost(s())).toBe(0);
  });

  it('subtracts the cache cost when excludeCache is set', () => {
    const sess = s({ cost_usd: 1.0, cost_cache_read: 0.3, cost_cache_create: 0.1 });
    expect(sessionCost(sess, true)).toBeCloseTo(0.6);
  });

  it('falls back to summing by_model cache cost when session-level is absent', () => {
    const sess = s({
      cost_usd: 1.0,
      by_model: [
        { cost_cache_read: 0.2, cost_cache_create: 0.05 },
        { cost_cache_read: 0.1, cost_cache_create: 0 },
      ] as never,
    });
    // total 1.0 − cache (0.35) = 0.65
    expect(sessionCost(sess, true)).toBeCloseTo(0.65);
  });

  it('clamps to zero when the cache estimate exceeds the total', () => {
    const sess = s({ cost_usd: 0.1, cost_cache_read: 0.3, cost_cache_create: 0 });
    expect(sessionCost(sess, true)).toBe(0);
  });

  it('returns the full total when excluding cache but no cache cost is recorded', () => {
    // No session-level cache fields and no by_model → cache resolves to 0.
    expect(sessionCost(s({ cost_usd: 0.8 }), true)).toBeCloseTo(0.8);
  });

  it('treats a missing cache-cost half as zero (only one side present)', () => {
    // cost_cache_create absent.
    expect(sessionCost(s({ cost_usd: 1.0, cost_cache_read: 0.25 }), true)).toBeCloseTo(0.75);
    // cost_cache_read absent (the other half).
    expect(sessionCost(s({ cost_usd: 1.0, cost_cache_create: 0.25 }), true)).toBeCloseTo(0.75);
  });

  it('excludes cache via by_model when cost_usd is absent and only some models carry cost', () => {
    const sess = s({
      by_model: [
        { cost_usd: 0.6, cost_cache_read: 0.2 }, // cache_create absent
        { tokens_total: 10 }, // no cost_usd at all → contributes 0 to the total
      ] as never,
    });
    // total = 0.6 (second model has no cost_usd) − cache 0.2 = 0.4
    expect(sessionCost(sess, true)).toBeCloseTo(0.4);
  });
});

describe('bucketCost', () => {
  it('returns cost_usd when not excluding cache', () => {
    expect(bucketCost({ cost_usd: 0.5 })).toBe(0.5);
  });

  it('drops the cache portion (clamped) when excluding cache', () => {
    expect(bucketCost({ cost_usd: 0.5, cost_cache_read: 0.2, cost_cache_create: 0.1 }, true)).toBeCloseTo(
      0.2,
    );
    expect(bucketCost({ cost_usd: 0.1, cost_cache_read: 0.5 }, true)).toBe(0);
  });

  it('treats a missing total or cache half as zero', () => {
    // No cost_usd and no cache → 0 either way.
    expect(bucketCost({}, true)).toBe(0);
    expect(bucketCost({})).toBe(0);
    // Only cost_cache_create present (read absent) still subtracts cleanly.
    expect(bucketCost({ cost_usd: 0.4, cost_cache_create: 0.1 }, true)).toBeCloseTo(0.3);
  });
});

describe('sessionCachePct', () => {
  it('prefers the server-computed cache_pct', () => {
    expect(sessionCachePct(s({ cache_pct: 87 }))).toBe(87);
    expect(sessionCachePct(s({ cache_pct: 0 }))).toBe(0); // 0 is a valid value, not "missing"
  });

  it('derives from the token breakdown when cache_pct is absent', () => {
    const sess = s({ tokens_total: 1000, tokens_cache_read: 700, tokens_cache_create: 100 });
    expect(sessionCachePct(sess)).toBe(80);
  });

  it('returns null when there are no tokens to report on', () => {
    expect(sessionCachePct(s())).toBeNull();
    expect(sessionCachePct(s({ tokens_total: 0 }))).toBeNull();
  });
});
