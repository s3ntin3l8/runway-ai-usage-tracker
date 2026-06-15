// Shared session metric helpers — used by both the Overview "Recent sessions"
// cards and the Activity "Top sessions" table so the two views can't drift.

import type { SessionEntry } from '@/api/types';

// Total tokens for a session. When excludeCache is set, drop cache read/write
// from whichever source fed the total so the number stays internally consistent.
export function sessionTokens(s: SessionEntry, excludeCache = false): number {
  const total =
    s.tokens_total ?? (s.by_model ?? []).reduce((sum, m) => sum + (m.tokens_total ?? 0), 0);
  if (!excludeCache) return total;
  const cache =
    s.tokens_total != null
      ? (s.tokens_cache_read ?? 0) + (s.tokens_cache_create ?? 0)
      : (s.by_model ?? []).reduce(
          (sum, m) => sum + (m.tokens_cache_read ?? 0) + (m.tokens_cache_create ?? 0),
          0,
        );
  return total - cache;
}

// Cache portion of a cost-bearing bucket (cache_read + cache_create). Structural
// type so it works for model splits, sidecar buckets, and subagent splits alike.
type CostCacheParts = { cost_cache_read?: number; cost_cache_create?: number };
export function modelCacheCost(b: CostCacheParts): number {
  return (b.cost_cache_read ?? 0) + (b.cost_cache_create ?? 0);
}

// Total cost for a cost-bearing bucket, optionally dropping the cache portion
// (clamped at 0). Used for per-model / per-subagent rows in detail panels.
export function bucketCost(b: { cost_usd?: number } & CostCacheParts, excludeCache = false): number {
  const total = b.cost_usd ?? 0;
  return excludeCache ? Math.max(0, total - modelCacheCost(b)) : total;
}

// Total cost for a session. When excludeCache is set, drop the cache-read/write
// portion (clamped at 0 — for provider-supplied totals the pricing-derived cache
// cost is a best-effort estimate that could rarely exceed cost_usd). Mirrors
// modelCost() in CostDonut.tsx so the two views stay consistent.
export function sessionCost(s: SessionEntry, excludeCache = false): number {
  const total = s.cost_usd ?? (s.by_model ?? []).reduce((sum, m) => sum + (m.cost_usd ?? 0), 0);
  if (!excludeCache) return total;
  const cache =
    s.cost_cache_read != null || s.cost_cache_create != null
      ? (s.cost_cache_read ?? 0) + (s.cost_cache_create ?? 0)
      : (s.by_model ?? []).reduce((sum, m) => sum + modelCacheCost(m), 0);
  return Math.max(0, total - cache);
}

// Cache share of total tokens (0–100). Prefers the server-computed `cache_pct`;
// falls back to deriving it from the token breakdown. Null when there are no
// tokens (nothing meaningful to report).
export function sessionCachePct(s: SessionEntry): number | null {
  if (typeof s.cache_pct === 'number') return s.cache_pct;
  const total = s.tokens_total ?? 0;
  if (total <= 0) return null;
  const cache = (s.tokens_cache_read ?? 0) + (s.tokens_cache_create ?? 0);
  return Math.round((cache / total) * 100);
}
