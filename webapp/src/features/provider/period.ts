// Shared month/period model for the provider detail tabs. The selected month
// lives in the `?period=YYYY-MM` URL search param (deep-linkable, mirrors the
// existing `tab`/`account` params); omitting it means the current month.
//
// All boundaries are tz-correct on the user's timezone (see tz.ts), matching
// the live current-month gauge and the backend's local-calendar anchoring.

import { currentYearMonth, endOfMonthISO, getUserTz, startOfMonthISO } from '@/lib/tz';
import type { DateRange } from './queries';

export interface SelectedPeriod {
  /** 'YYYY-MM' — the canonical key used in the URL and cumulative lookups. */
  key: string;
  year: number;
  /** 1-based month. */
  month: number;
  /** True when the key is the user's current local month. */
  isCurrentMonth: boolean;
  /** [since, until) UTC instants bounding the month, for range-scoped queries. */
  range: DateRange;
}

export function monthKey(year: number, month: number): string {
  return `${year}-${String(month).padStart(2, '0')}`;
}

export function currentMonthKey(): string {
  const { year, month } = currentYearMonth();
  return monthKey(year, month);
}

// Parse a 'YYYY-MM' key into a fully-resolved period. Falls back to the current
// month when the key is missing or malformed (defensive against bad deep-links).
export function resolvePeriod(key: string | null | undefined): SelectedPeriod {
  const cur = currentYearMonth();
  let year = cur.year;
  let month = cur.month;
  if (key && /^\d{4}-\d{2}$/.test(key)) {
    const [y, m] = key.split('-').map(Number);
    if (m >= 1 && m <= 12) {
      year = y;
      month = m;
    }
  }
  return {
    key: monthKey(year, month),
    year,
    month,
    isCurrentMonth: year === cur.year && month === cur.month,
    range: { since: startOfMonthISO(year, month), until: endOfMonthISO(year, month) },
  };
}

// Step a 'YYYY-MM' key by ±1 month, handling year rollover.
export function shiftMonthKey(key: string, delta: number): string {
  const [y, m] = key.split('-').map(Number);
  const zeroBased = (y * 12 + (m - 1)) + delta;
  return monthKey(Math.floor(zeroBased / 12), (zeroBased % 12) + 1);
}

// The 'YYYY-MM' month an ISO instant falls in, on the user's timezone. Used to
// derive the selector's lower bound from the earliest recorded event.
export function monthKeyOfISO(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: getUserTz(),
    year: 'numeric',
    month: 'numeric',
  }).formatToParts(new Date(iso));
  const year = Number(parts.find((p) => p.type === 'year')?.value);
  const month = Number(parts.find((p) => p.type === 'month')?.value);
  return monthKey(year, month);
}
