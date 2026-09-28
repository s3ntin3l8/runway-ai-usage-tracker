import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setTzConfig } from '@/lib/tz';
import { monthSpanForKey, parseRangeParam } from '@/lib/timeRange';
import { monthKey, resolveScope } from './period';

beforeEach(() => setTzConfig({ user_timezone: 'UTC', env_timezone: null }));
afterEach(() => vi.useRealTimers());

describe('monthKey', () => {
  it('zero-pads the month', () => {
    expect(monthKey(2026, 3)).toBe('2026-03');
    expect(monthKey(2026, 12)).toBe('2026-12');
  });
});

describe('resolveScope', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-14T12:00:00Z'));
    setTzConfig({ user_timezone: 'UTC', env_timezone: null });
  });

  it('resolves a month-aligned range to a month scope with a long label', () => {
    const s = resolveScope(monthSpanForKey('2026-03'));
    expect(s.key).toBe('2026-03-01_2026-03-31');
    expect(s.label).toBe('March 2026');
    expect(s.periodKey).toBe('2026-03');
    expect(s.isLiveMonth).toBe(false);
    expect(s.range.since).toBe('2026-03-01T00:00:00.000Z');
    expect(s.range.until).toBe('2026-04-01T00:00:00.000Z');
  });

  it('marks a span of the live calendar month', () => {
    const live = resolveScope(monthSpanForKey('2026-06'));
    expect(live.isLiveMonth).toBe(true);
    expect(live.periodKey).toBe('2026-06');
    expect(live.label).toBe('June 2026');
  });

  it('defaults to the last 7 days for omitted / malformed input', () => {
    const d = resolveScope(null);
    expect(d.value).toEqual({ days: 7 });
    expect(d.key).toBe('7d');
    expect(d.label).toBe('Last 7 days');
    expect(d.isLiveMonth).toBe(false);
    expect(d.periodKey).toBeUndefined();

    expect(resolveScope({} as never).key).toBe('7d');
    expect(resolveScope(parseRangeParam('garbage')).key).toBe('7d');
  });

  it('resolves a rolling range to a [now − N days, now) window', () => {
    const s = resolveScope({ days: 30 });
    expect(s.key).toBe('30d');
    expect(s.label).toBe('Last 30 days');
    expect(s.isLiveMonth).toBe(false);
    expect(s.periodKey).toBeUndefined();
    expect(s.range.until).toBe('2026-06-14T12:00:00.000Z');
    expect(s.range.since).toBe('2026-05-15T12:00:00.000Z');
  });

  it('resolves a custom range without a month periodKey', () => {
    const s = resolveScope({ since: '2026-06-01', until: '2026-06-15' });
    expect(s.label).toBe('Jun 1 – Jun 15');
    expect(s.periodKey).toBeUndefined();
    expect(s.isLiveMonth).toBe(false);
    expect(s.range.since).toBe('2026-06-01T00:00:00.000Z');
    expect(s.range.until).toBe('2026-06-16T00:00:00.000Z');
  });

  it('keeps the resolved value so widgets can re-serialize it', () => {
    expect(resolveScope({ days: 90 }).value).toEqual({ days: 90 });
    const abs = { since: '2026-05-01', until: '2026-05-31' };
    expect(resolveScope(abs).value).toEqual(abs);
  });
});
