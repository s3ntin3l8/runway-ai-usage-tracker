import { describe, expect, it } from 'vitest';
import type { LimitCard } from '@/api/types';
import {
  cardPct,
  cardStatus,
  chipLabel,
  clusterModelLabel,
  clusterPools,
  modelLabel,
  sameQuota,
  statusForPct,
  windowLabel,
} from './quota';

const card = (overrides: Partial<LimitCard>): LimitCard => ({
  service_name: 'Test',
  ...overrides,
});

describe('clusterPools', () => {
  it('groups cards sharing a quota_pool_id and keeps singletons apart', () => {
    const a = card({ service_name: 'Sonnet', quota_pool_id: 'anthropic:session:x' });
    const b = card({ service_name: 'Opus', quota_pool_id: 'anthropic:session:x' });
    const c = card({ service_name: 'Flash', quota_pool_id: null });
    const d = card({ service_name: 'Flash-Lite', quota_pool_id: null });
    const clusters = clusterPools([a, b, c, d]);
    expect(clusters).toHaveLength(3);
    expect(clusters[0]).toEqual([a, b]);
    // null pool ids must never cluster (Gemini Flash/Flash-Lite trap)
    expect(clusters[1]).toEqual([c]);
    expect(clusters[2]).toEqual([d]);
  });
});

describe('clusterModelLabel', () => {
  it('strips AG: prefixes and truncates beyond two names', () => {
    const cluster = [
      card({ service_name: 'AG: Sonnet' }),
      card({ service_name: 'Opus' }),
      card({ service_name: 'Haiku' }),
    ];
    expect(clusterModelLabel(cluster)).toBe('Sonnet · Opus +1');
  });
});

describe('status semantics', () => {
  it('maps pct thresholds', () => {
    expect(statusForPct(95)).toBe('critical');
    expect(statusForPct(75)).toBe('warning');
    expect(statusForPct(10)).toBe('ok');
    expect(statusForPct(null)).toBe('unknown');
  });
  it('error and unlimited cards take precedence', () => {
    expect(cardStatus(card({ error_type: 'auth', pct_used: 5 }))).toBe('critical');
    expect(cardStatus(card({ is_unlimited: true, pct_used: 95 }))).toBe('unlimited');
  });
  it('honours collector-asserted health over percentages', () => {
    expect(cardStatus(card({ health: 'warning', pct_used: 5 }))).toBe('warning');
  });
});

describe('sameQuota', () => {
  it('only matches equal, non-null pool ids', () => {
    expect(sameQuota(card({ quota_pool_id: 'x' }), card({ quota_pool_id: 'x' }))).toBe(true);
    expect(sameQuota(card({ quota_pool_id: 'x' }), card({ quota_pool_id: 'y' }))).toBe(false);
    expect(sameQuota(card({ quota_pool_id: null }), card({ quota_pool_id: null }))).toBe(false);
  });
});

describe('cardPct (the null-pct trap)', () => {
  it('prefers an explicit pct_used, including zero', () => {
    expect(cardPct(card({ pct_used: 42 }))).toBe(42);
    expect(cardPct(card({ pct_used: 0 }))).toBe(0);
  });
  it('derives from used/limit when pct_used is missing', () => {
    expect(cardPct(card({ used_value: 25, limit_value: 100 }))).toBe(25);
  });
  it('returns null when there is nothing to derive from', () => {
    expect(cardPct(card({}))).toBeNull();
    expect(cardPct(card({ used_value: 5, limit_value: 0 }))).toBeNull(); // no divide-by-zero
  });
});

describe('windowLabel', () => {
  it('title-cases known windows and nulls unknown ones', () => {
    expect(windowLabel(card({ window_type: 'weekly' }))).toBe('Weekly');
    expect(windowLabel(card({ window_type: 'unknown' }))).toBeNull();
    expect(windowLabel(card({}))).toBeNull();
  });
});

describe('chipLabel', () => {
  it('uses the window when sibling service names collide', () => {
    const weekly = card({ service_name: 'Claude', window_type: 'weekly' });
    const session = card({ service_name: 'Claude', window_type: 'session' });
    expect(chipLabel(weekly, [weekly, session])).toBe('Weekly');
  });
  it('uses the service name when it is unique, appending any variant', () => {
    const a = card({ service_name: 'Opus', window_type: 'weekly' });
    const b = card({ service_name: 'Sonnet', window_type: 'weekly', variant: '1M' });
    expect(chipLabel(a, [a, b])).toBe('Opus');
    expect(chipLabel(b, [a, b])).toBe('Sonnet 1M');
  });
  it('appends the model when two same-name windows collide (Claude weekly vs Sonnet weekly)', () => {
    const generic = card({ service_name: 'Claude', window_type: 'weekly', model_id: null });
    const sonnet = card({ service_name: 'Claude', window_type: 'weekly', model_id: 'sonnet' });
    expect(chipLabel(generic, [generic, sonnet])).toBe('Weekly');
    expect(chipLabel(sonnet, [generic, sonnet])).toBe('Weekly · Sonnet');
  });
  it('does not append a model that already is the label', () => {
    const c = card({ service_name: 'Sonnet', window_type: 'weekly', model_id: 'sonnet' });
    expect(chipLabel(c, [c])).toBe('Sonnet');
  });
});

describe('modelLabel', () => {
  it('humanizes model ids', () => {
    expect(modelLabel('sonnet')).toBe('Sonnet');
    expect(modelLabel('gemini-flash')).toBe('Gemini Flash');
    expect(modelLabel('claude_opus')).toBe('Claude Opus');
  });
});
