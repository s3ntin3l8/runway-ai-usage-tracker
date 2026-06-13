import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  formatLocalDate,
  formatLocalDateTime,
  formatLocalFromEpoch,
  formatLocalTime,
  getUserTz,
  setTzConfig,
  startOfCurrentMonthISO,
} from './tz';

// getUserTz() memoizes; setTzConfig() clears the cache, so reset before each
// test to keep them independent.
beforeEach(() => setTzConfig({ user_timezone: null, env_timezone: null }));

describe('getUserTz resolution chain', () => {
  it('prefers the explicit user_timezone', () => {
    setTzConfig({ user_timezone: 'America/New_York', env_timezone: 'Europe/Berlin' });
    expect(getUserTz()).toBe('America/New_York');
  });

  it('falls back to env_timezone when no user override', () => {
    setTzConfig({ user_timezone: null, env_timezone: 'Europe/Berlin' });
    expect(getUserTz()).toBe('Europe/Berlin');
  });

  it('falls back to a non-empty browser/UTC zone when nothing is configured', () => {
    setTzConfig({ user_timezone: null, env_timezone: null });
    expect(getUserTz()).toBeTruthy();
  });

  it('re-resolves after setTzConfig clears the cache', () => {
    setTzConfig({ user_timezone: 'America/New_York' });
    expect(getUserTz()).toBe('America/New_York');
    setTzConfig({ user_timezone: 'Asia/Tokyo' });
    expect(getUserTz()).toBe('Asia/Tokyo');
  });
});

describe('format helpers', () => {
  it('return an em dash for nullish input', () => {
    expect(formatLocalTime(null)).toBe('—');
    expect(formatLocalTime(undefined)).toBe('—');
    expect(formatLocalDate(null)).toBe('—');
  });

  it('format a known instant in the configured zone', () => {
    setTzConfig({ user_timezone: 'UTC' });
    // 2026-06-13T09:05:00Z
    expect(formatLocalTime('2026-06-13T09:05:00Z', { hour: '2-digit', minute: '2-digit' })).toMatch(
      /09[:.]05/,
    );
  });

  it('formatLocalFromEpoch matches the equivalent ISO instant', () => {
    setTzConfig({ user_timezone: 'UTC' });
    const opts = { year: 'numeric', month: '2-digit', day: '2-digit' } as const;
    const epoch = Date.UTC(2026, 5, 13, 9, 5, 0) / 1000;
    // It delegates to formatLocalDateTime — assert that equivalence (locale-safe).
    expect(formatLocalFromEpoch(epoch, opts)).toBe(
      formatLocalDateTime('2026-06-13T09:05:00Z', opts),
    );
  });
});

describe('startOfCurrentMonthISO', () => {
  afterEach(() => vi.useRealTimers());

  it('returns the 1st of the current month at local midnight (UTC zone)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-13T15:30:00Z'));
    setTzConfig({ user_timezone: 'UTC' });
    expect(startOfCurrentMonthISO()).toBe('2026-06-01T00:00:00.000Z');
  });

  it('accounts for the tz offset so local midnight maps to the right UTC instant', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-13T15:30:00Z'));
    setTzConfig({ user_timezone: 'America/New_York' });
    // June → EDT (UTC-4): local 2026-06-01T00:00 == 2026-06-01T04:00Z.
    expect(startOfCurrentMonthISO()).toBe('2026-06-01T04:00:00.000Z');
  });
});
