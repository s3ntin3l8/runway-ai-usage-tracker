import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setTzConfig } from './tz';
import {
  daysInMonth,
  formatRangeLabel,
  isLiveMonth,
  isRolling,
  monthAligned,
  monthSpan,
  monthSpanForKey,
  parseRangeParam,
  quickRanges,
  rangeQueryParams,
  resolveRange,
  serializeRangeParam,
} from './timeRange';

beforeEach(() => {
  setTzConfig({ user_timezone: 'UTC', env_timezone: null });
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-06-13T15:30:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
  setTzConfig({ user_timezone: null, env_timezone: null });
});

describe('parseRangeParam', () => {
  it('falls back to the default last-7-days window', () => {
    expect(parseRangeParam(null)).toEqual({ days: 7 });
    expect(parseRangeParam(undefined)).toEqual({ days: 7 });
    expect(parseRangeParam('')).toEqual({ days: 7 });
    expect(parseRangeParam('nonsense')).toEqual({ days: 7 });
    expect(parseRangeParam('0d')).toEqual({ days: 7 });
    expect(parseRangeParam('400d')).toEqual({ days: 7 });
  });

  it('parses rolling keys', () => {
    expect(parseRangeParam('7d')).toEqual({ days: 7 });
    expect(parseRangeParam('90d')).toEqual({ days: 90 });
  });

  it('parses absolute spans, swapping inverted bounds', () => {
    expect(parseRangeParam('2026-09-01_2026-09-30')).toEqual({
      since: '2026-09-01',
      until: '2026-09-30',
    });
    expect(parseRangeParam('2026-09-30_2026-09-01')).toEqual({
      since: '2026-09-01',
      until: '2026-09-30',
    });
  });

  it('accepts the legacy ?period=YYYY-MM month key', () => {
    expect(parseRangeParam('2026-01')).toEqual({ since: '2026-01-01', until: '2026-01-31' });
    expect(parseRangeParam('2026-02')).toEqual({ since: '2026-02-01', until: '2026-02-28' });
  });
});

describe('serializeRangeParam', () => {
  it('omits the default range from the URL', () => {
    expect(serializeRangeParam({ days: 7 })).toBeNull();
  });

  it('serializes rolling and absolute ranges', () => {
    expect(serializeRangeParam({ days: 30 })).toBe('30d');
    expect(serializeRangeParam({ since: '2026-09-01', until: '2026-09-30' })).toBe(
      '2026-09-01_2026-09-30',
    );
  });

  it('round-trips through parseRangeParam', () => {
    for (const raw of ['30d', '2026-09-01_2026-09-30', '2026-01-05_2026-03-10']) {
      const value = parseRangeParam(raw);
      const serialized = serializeRangeParam(value);
      if (serialized !== null) expect(serializeRangeParam(parseRangeParam(serialized))).toBe(serialized);
    }
  });
});

describe('monthSpan', () => {
  it('builds this month and last month from the frozen wall clock', () => {
    expect(monthSpan(0)).toEqual({ since: '2026-06-01', until: '2026-06-30' });
    expect(monthSpan(-1)).toEqual({ since: '2026-05-01', until: '2026-05-31' });
  });

  it('rolls across year boundaries', () => {
    expect(monthSpan(-6)).toEqual({ since: '2025-12-01', until: '2025-12-31' });
  });

  it('handles leap February via monthSpanForKey', () => {
    expect(monthSpanForKey('2028-02')).toEqual({ since: '2028-02-01', until: '2028-02-29' });
    expect(monthSpanForKey('2026-02')).toEqual({ since: '2026-02-01', until: '2026-02-28' });
    expect(monthSpanForKey('bad')).toEqual({ days: 7 });
  });

  it('daysInMonth matches calendar math', () => {
    expect(daysInMonth(2026, 6)).toBe(30);
    expect(daysInMonth(2026, 12)).toBe(31);
    expect(daysInMonth(2028, 2)).toBe(29);
  });
});

describe('resolveRange', () => {
  it('anchors rolling windows at now', () => {
    const { since, until } = resolveRange({ days: 7 });
    expect(until).toBe('2026-06-13T15:30:00.000Z');
    expect(since).toBe(new Date(Date.parse('2026-06-13T15:30:00.000Z') - 7 * 86_400_000).toISOString());
  });

  it('converts absolute dates to local-midnight instants with an exclusive end', () => {
    expect(resolveRange({ since: '2026-06-01', until: '2026-06-30' })).toEqual({
      since: '2026-06-01T00:00:00.000Z',
      until: '2026-07-01T00:00:00.000Z',
    });
  });
});

describe('rangeQueryParams', () => {
  it('emits days for rolling windows', () => {
    expect(rangeQueryParams({ days: 14 })).toEqual({ days: 14 });
    expect(rangeQueryParams({ days: 7 })).toEqual({ days: 7 });
  });

  it('emits instants for absolute spans', () => {
    expect(rangeQueryParams({ since: '2026-09-01', until: '2026-09-30' })).toEqual({
      since: '2026-09-01T00:00:00.000Z',
      until: '2026-10-01T00:00:00.000Z',
    });
  });

  it('falls back to the default for empty values', () => {
    expect(rangeQueryParams({})).toEqual({ days: 7 });
  });

  it('uses local calendar midnights with an exclusive upper bound across DST', () => {
    setTzConfig({ user_timezone: 'Europe/Berlin', env_timezone: null });
    expect(rangeQueryParams({ since: '2026-03-29', until: '2026-03-29' })).toEqual({
      since: '2026-03-28T23:00:00.000Z',
      until: '2026-03-29T22:00:00.000Z',
    });
  });
});

describe('monthAligned / isLiveMonth', () => {
  it('detects full-month spans', () => {
    expect(monthAligned({ since: '2026-06-01', until: '2026-06-30' })).toEqual({
      year: 2026,
      month: 6,
    });
    expect(monthAligned({ since: '2026-02-01', until: '2026-02-28' })).toEqual({
      year: 2026,
      month: 2,
    });
  });

  it('rejects partial or multi-month spans', () => {
    expect(monthAligned({ since: '2026-06-01', until: '2026-06-15' })).toBeNull();
    expect(monthAligned({ since: '2026-06-05', until: '2026-06-30' })).toBeNull();
    expect(monthAligned({ since: '2026-05-15', until: '2026-06-15' })).toBeNull();
    expect(monthAligned({ days: 7 })).toBeNull();
  });

  it('isLiveMonth only for the current calendar month', () => {
    expect(isLiveMonth({ since: '2026-06-01', until: '2026-06-30' })).toBe(true);
    expect(isLiveMonth({ since: '2026-05-01', until: '2026-05-31' })).toBe(false);
    expect(isLiveMonth({ days: 7 })).toBe(false);
  });
});

describe('formatRangeLabel', () => {
  it('labels rolling windows', () => {
    expect(formatRangeLabel({ days: 7 })).toBe('Last 7 days');
    expect(formatRangeLabel({ days: 90 })).toBe('Last 90 days');
  });

  it('labels month-aligned spans with the long month + year', () => {
    expect(formatRangeLabel({ since: '2026-06-01', until: '2026-06-30' })).toBe('June 2026');
    expect(formatRangeLabel(monthSpanForKey('2026-01'))).toBe('January 2026');
  });

  it('labels custom spans with short dates', () => {
    expect(formatRangeLabel({ since: '2026-06-01', until: '2026-06-27' })).toBe('Jun 1 – Jun 27');
  });

  it('includes the year when the span crosses years', () => {
    expect(formatRangeLabel({ since: '2025-12-15', until: '2026-01-05' })).toBe(
      'Dec 15, 2025 – Jan 5, 2026',
    );
  });
});

describe('quickRanges', () => {
  it('offers rolling presets plus this/last month computed at call time', () => {
    const ranges = quickRanges();
    expect(ranges.map((r) => r.label)).toEqual([
      'Last 7 days',
      'Last 14 days',
      'Last 30 days',
      'Last 90 days',
      'This month',
      'Last month',
    ]);
    expect(ranges[4].value).toEqual({ since: '2026-06-01', until: '2026-06-30' });
    expect(ranges[5].value).toEqual({ since: '2026-05-01', until: '2026-05-31' });
  });
});

describe('isRolling', () => {
  it('distinguishes rolling from absolute values', () => {
    expect(isRolling({ days: 7 })).toBe(true);
    expect(isRolling({ since: '2026-06-01', until: '2026-06-30' })).toBe(false);
    expect(isRolling({})).toBe(false);
  });
});
