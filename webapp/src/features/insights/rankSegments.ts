// Shared stacked-bar builders for the Insights ranking cards.
//
// The cards differ in *what* they rank (projects, sessions, hosts) — not in
// how a token or cost bar is split. The segment construction therefore lives
// here instead of being copied into every card, and the totals helpers keep
// the bar length and the sort key in agreement (the backend sorts by the
// same quantity the bar renders).

import type { RankSegment } from '@/components/charts/RankBar';
import { COST_SEGMENT_KEYS, TOKEN_SEGMENT_KEYS } from '@/components/charts/segmentDefs';

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

/** Token segments for one entry; cache segments are zeroed when excluded. */
export function tokenSegments(
  t: RankTotals,
  series: string[],
  excludeCache: boolean,
): RankSegment[] {
  const segments = TOKEN_SEGMENT_KEYS.map((k, i) => ({
    key: k.key,
    label: k.label,
    color: series[i],
    value: TOKEN_ACCESSORS[k.key]?.(t) ?? 0,
  }));
  if (excludeCache) {
    for (const s of segments) {
      if (s.key.startsWith('tokens_cache')) s.value = 0;
    }
  }
  return segments;
}

/** Cost segments for one entry; cache segments are zeroed when excluded. */
export function costSegments(
  t: RankTotals,
  series: string[],
  excludeCache: boolean,
): RankSegment[] {
  const segments = COST_SEGMENT_KEYS.map((k, i) => ({
    key: k.key,
    label: k.label,
    color: series[i],
    value: COST_ACCESSORS[k.key]?.(t) ?? 0,
  }));
  if (excludeCache) {
    for (const s of segments) {
      if (s.key.startsWith('cost_cache')) s.value = 0;
    }
  }
  return segments;
}

/** Bar length for the token metric (matches the backend's sort key). */
export function tokenTotal(t: RankTotals, excludeCache: boolean): number {
  const cache = t.tokens_cache_read + t.tokens_cache_create;
  return t.tokens_input + t.tokens_output + t.tokens_reasoning + (excludeCache ? 0 : cache);
}

/** Bar length for the cost metric (matches the backend's sort key). */
export function costTotal(t: RankTotals, excludeCache: boolean): number {
  return t.cost_usd - (excludeCache ? t.cost_cache : 0);
}
