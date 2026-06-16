// Shared month/period model for the provider detail tabs. The selected month
// lives in the `?period=YYYY-MM` URL search param (deep-linkable, mirrors the
// existing `tab`/`account` params); omitting it means the current month.
//
// All boundaries are tz-correct on the user's timezone (see tz.ts), matching
// the live current-month gauge and the backend's local-calendar anchoring.

import {
  currentYearMonth,
  endOfMonthISO,
  formatLocalDate,
  getUserTz,
  startOfMonthISO,
} from '@/lib/tz';
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

// --- Tab scope (month ⟷ rolling) -------------------------------------------
//
// The provider data tabs scope to either a calendar month OR a rolling window
// of the last N days. Both modes resolve to a single `TabScope` with a
// [since, until) range + a human label, so every widget on a tab honours one
// authoritative scope (see issue #87). The `?period=` URL param carries either
// a 'YYYY-MM' month key or a 'Nd' rolling key (e.g. '30d'); an omitted/bad
// value falls back to the current month.

export type ScopeMode = 'month' | 'rolling';

/** Rolling-window options offered by the scope toggle (days). */
export const ROLLING_DAYS = [7, 14, 30, 90] as const;
/** Default rolling window when switching Month → Rolling. */
export const DEFAULT_ROLLING_DAYS = 30;

export interface TabScope {
  mode: ScopeMode;
  /** URL value & identity for page-reset effects: 'YYYY-MM' | 'Nd'. */
  key: string;
  /** Display label: 'June 2026' | 'Last 30 days'. */
  label: string;
  /** [since, until) instants bounding the scope, for range-scoped queries. */
  range: DateRange;
  /** True only for the live calendar month — gates forward-looking projections. */
  isCurrentMonth: boolean;
  /** Calendar month context (month mode only). */
  year?: number;
  month?: number;
  /** Rolling window size in days (rolling mode only). */
  days?: number;
}

const ROLLING_KEY_RE = /^(\d+)d$/;

/** The 'Nd' URL/identity key for a rolling window of `days`. */
export function rollingKey(days: number): string {
  return `${days}d`;
}

/** Whether a key string is a recognised rolling-window key ('7d'/'14d'/…). */
export function isRollingKey(key: string | null | undefined): boolean {
  if (!key) return false;
  const m = ROLLING_KEY_RE.exec(key);
  return !!m && (ROLLING_DAYS as readonly number[]).includes(Number(m[1]));
}

// Resolve a `?period=` value into a fully-scoped `TabScope`. A 'Nd' key yields a
// rolling [now − N days, now) window; anything else is delegated to
// `resolvePeriod` (calendar month, current-month fallback for bad input).
export function resolveScope(key: string | null | undefined): TabScope {
  const m = key ? ROLLING_KEY_RE.exec(key) : null;
  if (m && (ROLLING_DAYS as readonly number[]).includes(Number(m[1]))) {
    const days = Number(m[1]);
    const until = new Date();
    const since = new Date(until.getTime() - days * 86_400_000);
    return {
      mode: 'rolling',
      key: rollingKey(days),
      label: `Last ${days} days`,
      range: { since: since.toISOString(), until: until.toISOString() },
      isCurrentMonth: false,
      days,
    };
  }
  const p = resolvePeriod(key);
  return {
    mode: 'month',
    key: p.key,
    label: formatLocalDate(p.range.since, { month: 'long', year: 'numeric' }),
    range: p.range,
    isCurrentMonth: p.isCurrentMonth,
    year: p.year,
    month: p.month,
  };
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
