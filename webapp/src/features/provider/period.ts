// Shared time-scope model for the provider detail tabs. The selected range
// lives in the `?range=` URL search param (see useRangeParam); the legacy
// `?period=` value is accepted as a deep-link fallback. Every widget on a tab
// derives its window from one `TabScope` (see issue #87). All boundaries are
// tz-correct on the user's timezone (see tz.ts), matching the live current-
// month gauge and the backend's local-calendar anchoring.

import {
  DEFAULT_RANGE,
  formatRangeLabel,
  isLiveMonth as rangeSpansLiveMonth,
  monthAligned,
  resolveRange,
  serializeRangeParam,
  type DateRangeValue,
} from '@/lib/timeRange';
import type { DateRange } from './queries';

export interface TabScope {
  /** The picker's value — the single source of truth for every widget. */
  value: DateRangeValue;
  /** URL identity for page-reset effects: '7d' | '30d' | 'YYYY-MM-DD_…'. */
  key: string;
  /** Display label: 'Last 7 days' | 'September 2026' | 'Sep 1 – Sep 27'. */
  label: string;
  /** [since, until) instants bounding the scope, for range-scoped queries. */
  range: DateRange;
  /** True only when the scope spans the live calendar month — gates
   * forward-looking projections and the live cumulative bucket. */
  isLiveMonth: boolean;
  /** 'YYYY-MM' when the scope is a full calendar month (drives the month
   * bucket lookup); absent for rolling or custom ranges. */
  periodKey?: string;
}

export function monthKey(year: number, month: number): string {
  return `${year}-${String(month).padStart(2, '0')}`;
}

// Resolve a range value (from `?range=` / legacy `?period=`) into a fully
// scoped `TabScope`. A missing or malformed value falls back to the default
// last-7-days window.
export function resolveScope(value: DateRangeValue | null | undefined): TabScope {
  const v: DateRangeValue =
    value && (value.days != null || (value.since && value.until)) ? value : { ...DEFAULT_RANGE };
  const aligned = monthAligned(v);
  return {
    value: v,
    key: serializeRangeParam(v) ?? `${v.days ?? 7}d`,
    label: formatRangeLabel(v),
    range: resolveRange(v),
    isLiveMonth: rangeSpansLiveMonth(v),
    ...(aligned ? { periodKey: monthKey(aligned.year, aligned.month) } : {}),
  };
}
