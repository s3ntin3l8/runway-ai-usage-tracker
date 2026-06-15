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

export function sessionCost(s: SessionEntry): number {
  return s.cost_usd ?? (s.by_model ?? []).reduce((sum, m) => sum + (m.cost_usd ?? 0), 0);
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
