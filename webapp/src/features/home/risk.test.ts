import { describe, expect, it } from 'vitest';
import type { FleetEntry, ForecastEntry } from '@/api/types';
import { applyLayoutOrder, atRiskItems, buildRiskItems } from './risk';

const entry = (provider: string, pct: number | null, account = 'default'): FleetEntry => ({
  provider_id: provider,
  account_id: account,
  critical_gauge: { service_name: provider, pct_used: pct },
  secondary_limits: [],
});

const forecast = (
  provider: string,
  status: ForecastEntry['status'],
  projected = 100,
): ForecastEntry => ({
  provider_id: provider,
  account_id: 'default',
  status,
  projected_pct: projected,
});

describe('buildRiskItems', () => {
  it('flags hot gauges by threshold', () => {
    const items = buildRiskItems([entry('a', 95), entry('b', 75), entry('c', 20)], []);
    expect(items.map((i) => i.level)).toEqual(['critical', 'warning', 'ok']);
  });

  it('escalates a calm gauge when the forecast projects exhaustion', () => {
    const items = buildRiskItems([entry('a', 40)], [forecast('a', 'risk', 120)]);
    expect(items[0].level).toBe('critical');
    expect(items[0].forecast?.status).toBe('risk');
  });

  it('never downgrades a hot gauge because the forecast is calm', () => {
    const items = buildRiskItems([entry('a', 95)], [forecast('a', 'warn', 60)]);
    expect(items[0].level).toBe('critical');
  });
});

describe('atRiskItems', () => {
  it('filters ok and sorts most urgent first', () => {
    const items = buildRiskItems(
      [entry('low', 75), entry('high', 96), entry('calm', 5)],
      [],
    );
    const rail = atRiskItems(items);
    expect(rail.map((i) => i.entry.provider_id)).toEqual(['high', 'low']);
  });
});

describe('applyLayoutOrder', () => {
  it('orders by saved layout and appends unknown keys', () => {
    const items = buildRiskItems([entry('a', 1), entry('b', 2), entry('c', 3)], []);
    const ordered = applyLayoutOrder(items, ['b:default', 'a:default']);
    expect(ordered.map((i) => i.entry.provider_id)).toEqual(['b', 'a', 'c']);
  });
});
