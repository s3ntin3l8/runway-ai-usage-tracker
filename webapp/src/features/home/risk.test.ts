import { describe, expect, it } from 'vitest';
import type { FleetEntry, ForecastEntry, LimitCard } from '@/api/types';
import { applyLayoutOrder, atRiskItems, buildRiskItems, forecastLabel } from './risk';

const entry = (provider: string, pct: number | null, account = 'default'): FleetEntry => ({
  provider_id: provider,
  account_id: account,
  critical_gauge: { service_name: provider, pct_used: pct },
  secondary_limits: [],
});

// FleetEntry with multiple windows: a calm high-% Monthly critical_gauge and a
// risky lower-% Session secondary — mirrors the real OpenCode scenario.
const multiWindowEntry = (provider: string, account = 'default'): FleetEntry => ({
  provider_id: provider,
  account_id: account,
  critical_gauge: { service_name: provider, pct_used: 61, window_type: 'monthly' } as LimitCard,
  secondary_limits: [
    { service_name: provider, pct_used: 57, window_type: 'session' } as LimitCard,
    { service_name: provider, pct_used: 22, window_type: 'weekly' } as LimitCard,
  ],
});

const forecast = (
  provider: string,
  status: ForecastEntry['status'],
  projected = 100,
  window_type?: string,
): ForecastEntry => ({
  provider_id: provider,
  account_id: 'default',
  status,
  projected_pct: projected,
  ...(window_type ? { window_type } : {}),
} as ForecastEntry);

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

  it('features the session window when it has a risk forecast, even though monthly has higher pct', () => {
    // OpenCode scenario: Monthly 61% (critical_gauge, highest %), Session 57%
    // (secondary), Weekly 22%. Only Session has a risk forecast.
    const sessionForecast = forecast('oc', 'risk', 120, 'session');
    const monthlyForecast = forecast('oc', 'stable', 72, 'monthly');
    const items = buildRiskItems(
      [multiWindowEntry('oc')],
      [sessionForecast, monthlyForecast],
    );
    const item = items[0];

    // Lead window should be Session, not Monthly
    expect(item.lead.card.window_type).toBe('session');
    // The badge forecast matches the lead window
    expect(item.forecast?.window_type).toBe('session');
    expect(item.forecast?.status).toBe('risk');
    // Entry-level severity is still critical (driven by session forecast)
    expect(item.level).toBe('critical');
    // status stays tied to critical_gauge (monthly) for the grid chip colours
    expect(item.status).not.toBe('critical'); // monthly is only 61%, below thresholds
  });

  it('keeps monthly as lead when it is the hot gauge and session is calm', () => {
    // If monthly is 92% (hot gauge) and session is calm, monthly should lead.
    const hotEntry: FleetEntry = {
      provider_id: 'p',
      account_id: 'default',
      critical_gauge: { service_name: 'p', pct_used: 92, window_type: 'monthly' } as LimitCard,
      secondary_limits: [
        { service_name: 'p', pct_used: 30, window_type: 'session' } as LimitCard,
      ],
    };
    const items = buildRiskItems([hotEntry], [forecast('p', 'stable', 50, 'session')]);
    expect(items[0].lead.card.window_type).toBe('monthly');
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

describe('forecastLabel', () => {
  // Build a forecast where remaining headroom is exhausted at `seconds` from now
  // (slope is pct-per-second), so the glide branch is deterministic.
  const glide = (nowPct: number, seconds: number): ForecastEntry =>
    ({
      provider_id: 'a',
      account_id: 'default',
      status: 'warn',
      glide_pct: 100,
      now_pct: nowPct,
      slope: (100 - nowPct) / seconds,
    }) as ForecastEntry;

  it('returns null for no forecast', () => {
    expect(forecastLabel(null)).toBeNull();
  });

  it('reports exhausted directly', () => {
    expect(forecastLabel({ provider_id: 'a', account_id: 'default', status: 'exhausted' } as ForecastEntry)).toBe(
      'exhausted',
    );
  });

  it('renders the glide ETA in minutes / hours / days', () => {
    expect(forecastLabel(glide(50, 30 * 60))).toBe('limit in ~30m');
    expect(forecastLabel(glide(50, 4 * 3600))).toBe('limit in ~4h');
    expect(forecastLabel(glide(50, 3 * 86400))).toBe('limit in ~3d');
  });

  it('falls back to projected pct, then to the status text', () => {
    expect(
      forecastLabel({ provider_id: 'a', account_id: 'default', status: 'warn', projected_pct: 88 } as ForecastEntry),
    ).toBe('projected 88%');
    expect(
      forecastLabel({ provider_id: 'a', account_id: 'default', status: 'near_limit' } as ForecastEntry),
    ).toBe('near limit');
  });
});
