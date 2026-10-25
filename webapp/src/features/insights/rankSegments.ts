// Shared stacked-bar builders for the ranking cards.
//
// The cards differ in *what* they rank (projects, sessions, hosts, models) —
// not in how a token or cost bar is split. The segment construction therefore
// lives here instead of being copied into every card, and the totals helpers
// keep the bar length and the sort key in agreement (the backend sorts by the
// same quantity the bar renders).
//
// Colour: a segment is painted with the colour slot its ROLE owns in the
// global table (components/charts/segmentColors), so "Cache read" is the same
// colour in the token breakdown and in the cost breakdown.

import type { RankSegment } from '@/components/charts/RankBar';
import { SEGMENT_ROLE_SLOT } from '@/components/charts/segmentColors';
import { COST_SEGMENT_KEYS, TOKEN_SEGMENT_KEYS } from '@/components/charts/segmentDefs';

/** The metric a ranking bar counts. */
export type RankMetric = 'tokens' | 'cost';

/**
 * What happens to the cache segments when cache is excluded.
 *
 * - 'include': the cache segments are drawn with their values.
 * - 'zero': the cache segments stay in the chart but count zero, so the
 *   legend keeps naming them.
 * - 'omit': the cache segments are dropped from the chart and from the
 *   legend.
 */
export type CacheHandling = 'include' | 'zero' | 'omit';

/** The token/cost component fields every ranking entry carries. */
export interface RankTotals {
  tokens_input: number;
  tokens_output: number;
  tokens_reasoning: number;
  tokens_cache_read: number;
  tokens_cache_create: number;
  cost_usd: number;
  cost_cache: number;
  cost_input: number;
  cost_output: number;
  cost_cache_read: number;
  cost_cache_create: number;
}

const TOKEN_ACCESSORS: Record<string, (t: RankTotals) => number> = {
  tokens_input: (t) => t.tokens_input,
  tokens_output: (t) => t.tokens_output,
  tokens_reasoning: (t) => t.tokens_reasoning,
  tokens_cache_read: (t) => t.tokens_cache_read,
  tokens_cache_create: (t) => t.tokens_cache_create,
};

const COST_ACCESSORS: Record<string, (t: RankTotals) => number> = {
  cost_input: (t) => t.cost_input,
  cost_output: (t) => t.cost_output,
  cost_cache_read: (t) => t.cost_cache_read,
  cost_cache_create: (t) => t.cost_cache_create,
};

function isCacheKey(key: string): boolean {
  return key.startsWith('tokens_cache') || key.startsWith('cost_cache');
}

/**
 * The segments of one entry: the breakdown the bar draws.
 *
 * Every segment is painted with the colour slot its ROLE owns, so the colour
 * of a segment does not depend on the metric, on the position of the segment
 * in the list, or on whether the cache segments are still there.
 */
export function buildSegments(
  t: RankTotals,
  metric: RankMetric,
  series: string[],
  cache: CacheHandling = 'include',
): RankSegment[] {
  const defs = metric === 'cost' ? COST_SEGMENT_KEYS : TOKEN_SEGMENT_KEYS;
  const accessors = metric === 'cost' ? COST_ACCESSORS : TOKEN_ACCESSORS;
  const segments: RankSegment[] = [];
  for (const def of defs) {
    const isCache = isCacheKey(def.key);
    if (isCache && cache === 'omit') continue;
    segments.push({
      key: def.key,
      label: def.label,
      color: series[SEGMENT_ROLE_SLOT[def.role] - 1],
      value: isCache && cache === 'zero' ? 0 : (accessors[def.key]?.(t) ?? 0),
    });
  }
  return segments;
}

/** Token segments for one entry; cache segments are zeroed when excluded. */
export function tokenSegments(
  t: RankTotals,
  series: string[],
  excludeCache: boolean,
): RankSegment[] {
  return buildSegments(t, 'tokens', series, excludeCache ? 'zero' : 'include');
}

/** Cost segments for one entry; cache segments are zeroed when excluded. */
export function costSegments(
  t: RankTotals,
  series: string[],
  excludeCache: boolean,
): RankSegment[] {
  return buildSegments(t, 'cost', series, excludeCache ? 'zero' : 'include');
}

/** Bar length for the token metric (matches the backend's sort key). */
export function tokenTotal(t: RankTotals, excludeCache: boolean): number {
  const cache = t.tokens_cache_read + t.tokens_cache_create;
  return t.tokens_input + t.tokens_output + t.tokens_reasoning + (excludeCache ? 0 : cache);
}

/**
 * The backend's authoritative cost total (minus cache when excluded) — the
 * quantity the backend sorts by.
 *
 * It is NOT the sum of the cost components: a provider that reports its own
 * cost total makes the two differ (see app/models/db.py: the components
 * "sum ≈ cost_usd"). Charts disclose it next to the drawn total instead of
 * drawing a bar that contradicts the segments under it.
 */
export function costTotal(t: RankTotals, excludeCache: boolean): number {
  return t.cost_usd - (excludeCache ? t.cost_cache : 0);
}
