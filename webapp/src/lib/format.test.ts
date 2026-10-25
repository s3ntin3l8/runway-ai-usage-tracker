import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  APP_LOCALE,
  formatAxisCost,
  formatAxisNumber,
  formatCost,
  formatCount,
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
  it('uses 1 decimal on K (trailing zeros trimmed), 2 on M/B', () => {
    expect(formatTokens(1_500)).toBe('1.5K');
    expect(formatTokens(15_000)).toBe('15K');
    expect(formatTokens(2_440_000)).toBe('2.44M');
    expect(formatTokens(3_100_000_000)).toBe('3.10B');
  });
  it('keeps the same granularity as formatNumber', () => {
    // The audit finding: the same magnitude read as "1K" here and "1.5K"
    // there, on the same card.
    expect(formatTokens(1_400)).toBe('1.4K');
    expect(formatNumber(1_400)).toBe('1.4K');
  });
});

describe('formatCount', () => {
  it('renders an exact count with thousands separators', () => {
    expect(formatCount(1_500)).toBe('1,500');
    expect(formatCount(1_234_567)).toBe('1,234,567');
    expect(formatCount(0)).toBe('0');
  });
  it('does not compact into a magnitude', () => {
    expect(formatCount(1_500)).not.toBe('1.5K');
  });
  it('renders a dash for nullish / NaN', () => {
    expect(formatCount(null)).toBe('—');
    expect(formatCount(undefined)).toBe('—');
    expect(formatCount(NaN)).toBe('—');
  });
  it('is pinned to the app locale, not the browser locale', () => {
    expect(APP_LOCALE).toBe('en-US');
  });
});

describe('formatAxisNumber / formatAxisCost', () => {
  it('compacts without trailing zeros', () => {
    expect(formatAxisNumber(300_000_000)).toBe('300M');
    expect(formatAxisNumber(1_500_000_000)).toBe('1.5B');
    expect(formatAxisNumber(0)).toBe('0');
  });
  it('compacts currency', () => {
    expect(formatAxisCost(1200)).toBe('$1.2K');
    expect(formatAxisCost(300)).toBe('$300');
    expect(formatAxisCost(0)).toBe('$0');
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
  it('never rounds a share up onto 100%', () => {
    // The audit finding: 99.6% rendered "100%".
    expect(formatPct(99.6)).toBe('99.6%');
    expect(formatPct(99.96)).toBe('99.96%');
    expect(formatPct(99.999)).toBe('99.999%');
  });
  it('still renders a real 100% as 100%', () => {
    expect(formatPct(100)).toBe('100%');
    expect(formatPct(0)).toBe('0%');
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
