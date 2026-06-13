import { describe, expect, it } from 'vitest';
import type { LimitCard } from '@/api/types';
import { cardStatus, clusterModelLabel, clusterPools, statusForPct } from './quota';

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
});
