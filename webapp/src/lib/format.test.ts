import { describe, expect, it } from 'vitest';
import { formatDuration, formatNumber, formatTokens } from './format';

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
