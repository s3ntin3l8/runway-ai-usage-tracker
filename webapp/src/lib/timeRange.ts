// Unified time-range model shared by History, Insights, and the Provider tabs.
//
// A range is either a rolling window (`{ days: 7 }`) or an absolute calendar
// span (`{ since: '2026-09-01', until: '2026-09-30' }`, inclusive on both ends
// as calendar dates). The URL carries it as `?range=` — 'Nd' for rolling,
// 'YYYY-MM-DD_YYYY-MM-DD' for absolute; an omitted param means the default
// (last 7 days). The legacy Provider `?period=` value ('YYYY-MM' month key or
// 'Nd') is accepted as a fallback and normalized by `parseRangeParam`.

import {
  currentYearMonth,
  formatLocalDate,
  getUserTz,
  localDateStartISO,
  nextCalendarDate,
} from './tz';

export interface DateRangeValue {
  days?: number; // rolling window
  since?: string; // YYYY-MM-DD calendar date (absolute start, inclusive)
  until?: string; // YYYY-MM-DD calendar date (absolute end, inclusive)
}

export const DEFAULT_RANGE: DateRangeValue = { days: 7 };

/** Rolling-window sizes offered as quick ranges. */
export const ROLLING_DAYS = [7, 14, 30, 90] as const;

const MONTH_RE = /^(\d{4})-(\d{2})$/;
const SPAN_RE = /^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$/;
const ROLLING_RE = /^(\d{1,3})d$/;

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

/** Days in the given (1-based) month — pure calendar math, tz-independent. */
export function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

/** The absolute span of a calendar month (offset 0 = this month, -1 = last). */
export function monthSpan(offset = 0): DateRangeValue {
  const cur = currentYearMonth();
  const total = cur.year * 12 + (cur.month - 1) + offset;
  const year = Math.floor(total / 12);
  const month = (total % 12) + 1;
  return {
    since: `${year}-${pad2(month)}-01`,
    until: `${year}-${pad2(month)}-${pad2(daysInMonth(year, month))}`,
  };
}

/** The absolute span of a 'YYYY-MM' month key (also backs legacy `?period=`). */
export function monthSpanForKey(key: string): DateRangeValue {
  const m = MONTH_RE.exec(key);
  if (!m) return { ...DEFAULT_RANGE };
  const year = Number(m[1]);
  const month = Number(m[2]);
  if (month < 1 || month > 12) return { ...DEFAULT_RANGE };
  return {
    since: `${year}-${pad2(month)}-01`,
    until: `${year}-${pad2(month)}-${pad2(daysInMonth(year, month))}`,
  };
}

export interface QuickRange {
  id: string;
  label: string;
  value: DateRangeValue;
}

/**
 * Quick-range options, computed fresh so the month entries track the wall
 * clock (call at render time, not module load).
 */
export function quickRanges(): QuickRange[] {
  return [
    ...ROLLING_DAYS.map((days) => ({
      id: `${days}d`,
      label: `Last ${days} days`,
      value: { days } as DateRangeValue,
    })),
    { id: 'this-month', label: 'This month', value: monthSpan(0) },
    { id: 'last-month', label: 'Last month', value: monthSpan(-1) },
  ];
}

/**
 * Parse a `?range=` (or legacy `?period=`) param into a range value.
 * Unrecognised input falls back to the default last-7-days window.
 */
export function parseRangeParam(raw: string | null | undefined): DateRangeValue {
  if (!raw) return { ...DEFAULT_RANGE };
  const rolling = ROLLING_RE.exec(raw);
  if (rolling) {
    const days = Number(rolling[1]);
    if (days >= 1 && days <= 365) return { days };
    return { ...DEFAULT_RANGE };
  }
  const span = SPAN_RE.exec(raw);
  if (span) {
    const [a, b] = [span[1], span[2]];
    return a <= b ? { since: a, until: b } : { since: b, until: a };
  }
  if (MONTH_RE.test(raw)) return monthSpanForKey(raw); // legacy ?period=YYYY-MM
  return { ...DEFAULT_RANGE };
}

/**
 * Serialize a range value back to its `?range=` representation. The default
 * (last 7 days) serializes to null so the param stays out of the URL.
 */
export function serializeRangeParam(v: DateRangeValue): string | null {
  if (isRolling(v)) {
    return v.days === DEFAULT_RANGE.days ? null : `${v.days}d`;
  }
  if (isAbsolute(v)) return `${v.since}_${v.until}`;
  return null;
}

/** True for a rolling `{ days }` value (no absolute bounds). */
export function isRolling(v: DateRangeValue): boolean {
  return v.days != null && v.since == null;
}

/** True for an absolute `{ since, until }` value. */
export function isAbsolute(v: DateRangeValue): boolean {
  return typeof v.since === 'string' && typeof v.until === 'string';
}

/**
 * Resolve a range to `[since, until)` UTC instants — the shape the API
 * endpoints expect. Rolling windows anchor at "now"; absolute spans convert
 * their inclusive calendar dates to instants (local midnight, DST-correct).
 */
export function resolveRange(v: DateRangeValue): { since: string; until: string } {
  if (isRolling(v) && v.days != null) {
    const until = new Date();
    const since = new Date(until.getTime() - v.days * 86_400_000);
    return { since: since.toISOString(), until: until.toISOString() };
  }
  if (isAbsolute(v)) {
    return {
      since: localDateStartISO(v.since!),
      until: localDateStartISO(nextCalendarDate(v.until!)),
    };
  }
  return resolveRange(DEFAULT_RANGE);
}

/** API query params for a range: `{ days }` or `{ since, until }`. */
export function rangeQueryParams(v: DateRangeValue): Record<string, unknown> {
  if (isRolling(v) && v.days != null) return { days: v.days };
  if (isAbsolute(v)) {
    return {
      since: localDateStartISO(v.since!),
      until: localDateStartISO(nextCalendarDate(v.until!)),
    };
  }
  return { days: DEFAULT_RANGE.days as number };
}

/**
 * The calendar month an absolute range aligns to, if it spans exactly one
 * full calendar month (first → last day). Drives the month-bucket query path.
 */
export function monthAligned(v: DateRangeValue): { year: number; month: number } | null {
  if (!isAbsolute(v)) return null;
  const from = MONTH_RE.exec(`${v.since!.slice(0, 7)}`);
  const to = MONTH_RE.exec(`${v.until!.slice(0, 7)}`);
  if (!from || !to || from[1] !== to[1] || from[2] !== to[2]) return null;
  const year = Number(from[1]);
  const month = Number(from[2]);
  if (month < 1 || month > 12) return null;
  if (v.since!.slice(8, 10) !== '01') return null;
  if (v.until!.slice(8, 10) !== String(daysInMonth(year, month)).padStart(2, '0')) return null;
  return { year, month };
}

/** True when the range is an absolute span of the current calendar month. */
export function isLiveMonth(v: DateRangeValue): boolean {
  const aligned = monthAligned(v);
  if (!aligned) return false;
  const cur = currentYearMonth();
  return aligned.year === cur.year && aligned.month === cur.month;
}

/** Human label: 'Last 7 days' | 'September 2026' | 'Sep 1 – Sep 27'. */
export function formatRangeLabel(v: DateRangeValue): string {
  if (isRolling(v) && v.days != null) return `Last ${v.days} days`;
  if (isAbsolute(v)) {
    const aligned = monthAligned(v);
    if (aligned) {
      return formatLocalDate(localDateStartISO(v.since!), { month: 'long', year: 'numeric' });
    }
    const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' };
    const fromYear = v.since!.slice(0, 4);
    const toYear = v.until!.slice(0, 4);
    if (fromYear !== toYear) {
      opts.year = 'numeric';
    }
    return `${formatLocalDate(localDateStartISO(v.since!), opts)} – ${formatLocalDate(
      localDateStartISO(v.until!),
      opts,
    )}`;
  }
  return formatRangeLabel(DEFAULT_RANGE);
}

/** Today's calendar date (YYYY-MM-DD) in the resolved user timezone. */
export function todayISODate(): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: getUserTz(),
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date());
}

/** The calendar date a UTC instant falls on in the resolved user timezone. */
export function isoDateOfInstant(iso: string): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: getUserTz(),
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date(iso));
}
