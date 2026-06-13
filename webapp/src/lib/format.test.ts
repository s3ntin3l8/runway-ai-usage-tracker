import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  formatCost,
  formatCurrency,
  formatDuration,
  formatNumber,
  formatPct,
  formatTokens,
  timeAgo,
  timeUntil,
} from './format';

describe('formatNumber', () => {
  it('keeps integers untouched below 1K', () => {
    expect(formatNumber(999)).toBe('999');
  });
  it('compacts with one decimal', () => {
    expect(formatNumber(1_500)).toBe('1.5K');
    expect(formatNumber(2_400_000)).toBe('2.4M');
    expect(formatNumber(3_100_000_000)).toBe('3.1B');
  });
  it('renders dash for missing values', () => {
    expect(formatNumber(null)).toBe('—');
    expect(formatNumber(undefined)).toBe('—');
    expect(formatNumber(NaN)).toBe('—');
  });
});

describe('formatTokens', () => {
  it('renders explicit zero', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(null)).toBe('0');
  });
  it('uses 0 decimals on K, 2 on M/B', () => {
    expect(formatTokens(1_500)).toBe('2K');
    expect(formatTokens(2_440_000)).toBe('2.44M');
    expect(formatTokens(3_100_000_000)).toBe('3.10B');
  });
});

describe('formatDuration', () => {
  it('compacts to the two most significant units', () => {
    expect(formatDuration(30_000)).toBe('<1m');
    expect(formatDuration(12 * 60_000)).toBe('12m');
    expect(formatDuration(4 * 3_600_000 + 12 * 60_000)).toBe('4h 12m');
    expect(formatDuration(3 * 86_400_000 + 4 * 3_600_000)).toBe('3d 4h');
  });
  it('renders past boundaries as now', () => {
    expect(formatDuration(-5)).toBe('now');
  });
});

describe('formatCurrency', () => {
  it('renders a dash for nullish / NaN', () => {
    expect(formatCurrency(null)).toBe('—');
    expect(formatCurrency(undefined)).toBe('—');
    expect(formatCurrency(NaN)).toBe('—');
  });
  it('formats a USD amount', () => {
    expect(formatCurrency(12.5, 'USD')).toBe('$12.50');
  });
});

describe('formatCost', () => {
  it('distinguishes missing, zero, and sub-cent', () => {
    expect(formatCost(null)).toBe('—');
    expect(formatCost(0)).toBe('$0.00');
    expect(formatCost(0.004)).toBe('< $0.01');
  });
  it('formats a normal amount', () => {
    expect(formatCost(3.2)).toBe('$3.20');
  });
});

describe('formatPct', () => {
  it('renders a dash for nullish / NaN', () => {
    expect(formatPct(null)).toBe('—');
    expect(formatPct(NaN)).toBe('—');
  });
  it('honours the digits argument', () => {
    expect(formatPct(42.567)).toBe('43%');
    expect(formatPct(42.567, 1)).toBe('42.6%');
  });
});

describe('timeUntil / timeAgo', () => {
  afterEach(() => vi.useRealTimers());

  it('timeUntil returns null for missing / unparseable', () => {
    expect(timeUntil(null)).toBeNull();
    expect(timeUntil('not-a-date')).toBeNull();
  });

  it('timeUntil compacts the remaining duration', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-13T00:00:00Z'));
    expect(timeUntil('2026-06-13T04:12:00Z')).toBe('4h 12m');
  });

  it('timeAgo renders just now / dash / relative', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-13T00:00:00Z'));
    expect(timeAgo(null)).toBe('—');
    expect(timeAgo('bad')).toBe('—');
    expect(timeAgo('2026-06-12T23:59:30Z')).toBe('just now');
    expect(timeAgo('2026-06-12T20:00:00Z')).toBe('4h ago');
  });
});
