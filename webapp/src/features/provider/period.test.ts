import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setTzConfig } from '@/lib/tz';
import {
  currentMonthKey,
  isRollingKey,
  monthKey,
  monthKeyOfISO,
  resolvePeriod,
  resolveScope,
  shiftMonthKey,
} from './period';

beforeEach(() => setTzConfig({ user_timezone: 'UTC', env_timezone: null }));
afterEach(() => vi.useRealTimers());

describe('monthKey / shiftMonthKey', () => {
  it('zero-pads the month', () => {
    expect(monthKey(2026, 3)).toBe('2026-03');
    expect(monthKey(2026, 12)).toBe('2026-12');
  });

  it('steps months with year rollover', () => {
    expect(shiftMonthKey('2026-03', -1)).toBe('2026-02');
    expect(shiftMonthKey('2026-01', -1)).toBe('2025-12');
    expect(shiftMonthKey('2026-12', 1)).toBe('2027-01');
    expect(shiftMonthKey('2026-06', -6)).toBe('2025-12');
  });
});

describe('resolvePeriod', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-14T12:00:00Z'));
    setTzConfig({ user_timezone: 'UTC', env_timezone: null });
  });

  it('resolves a valid past key with tz-correct range and isCurrentMonth=false', () => {
    const p = resolvePeriod('2026-03');
    expect(p.key).toBe('2026-03');
    expect(p.year).toBe(2026);
    expect(p.month).toBe(3);
    expect(p.isCurrentMonth).toBe(false);
    expect(p.range.since).toBe('2026-03-01T00:00:00.000Z');
    expect(p.range.until).toBe('2026-04-01T00:00:00.000Z');
  });

  it('marks the current month', () => {
    expect(resolvePeriod('2026-06').isCurrentMonth).toBe(true);
    expect(resolvePeriod(null).isCurrentMonth).toBe(true);
  });

  it('falls back to the current month for a malformed key', () => {
    expect(resolvePeriod('garbage').key).toBe('2026-06');
    expect(resolvePeriod('2026-13').key).toBe('2026-06');
  });
});

describe('resolveScope', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-14T12:00:00Z'));
    setTzConfig({ user_timezone: 'UTC', env_timezone: null });
  });

  it('resolves a month key to a month-mode scope with a long label', () => {
    const s = resolveScope('2026-03');
    expect(s.mode).toBe('month');
    expect(s.key).toBe('2026-03');
    expect(s.label).toBe('March 2026');
    expect(s.isCurrentMonth).toBe(false);
    expect(s.range.since).toBe('2026-03-01T00:00:00.000Z');
  });

  it('defaults to the current month for omitted / malformed input', () => {
    expect(resolveScope(null).key).toBe('2026-06');
    expect(resolveScope(null).isCurrentMonth).toBe(true);
    expect(resolveScope('garbage').key).toBe('2026-06');
  });

  it('resolves a rolling key to a [now − N days, now) window', () => {
    const s = resolveScope('30d');
    expect(s.mode).toBe('rolling');
    expect(s.key).toBe('30d');
    expect(s.label).toBe('Last 30 days');
    expect(s.days).toBe(30);
    expect(s.isCurrentMonth).toBe(false);
    expect(s.range.until).toBe('2026-06-14T12:00:00.000Z');
    expect(s.range.since).toBe('2026-05-15T12:00:00.000Z');
  });

  it('treats an unsupported rolling window as a month fallback', () => {
    // 45d isn't an offered window → not a valid rolling key → current month.
    expect(isRollingKey('45d')).toBe(false);
    expect(resolveScope('45d').mode).toBe('month');
    expect(isRollingKey('30d')).toBe(true);
  });
});

describe('currentMonthKey / monthKeyOfISO', () => {
  it('currentMonthKey reflects the user tz', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-14T12:00:00Z'));
    setTzConfig({ user_timezone: 'UTC', env_timezone: null });
    expect(currentMonthKey()).toBe('2026-06');
  });

  it('monthKeyOfISO buckets an instant on the user tz', () => {
    setTzConfig({ user_timezone: 'America/New_York', env_timezone: null });
    // 2026-04-01T02:00Z is still 2026-03-31 in New York (UTC-4) → March.
    expect(monthKeyOfISO('2026-04-01T02:00:00Z')).toBe('2026-03');
    expect(monthKeyOfISO(null)).toBeNull();
  });
});
