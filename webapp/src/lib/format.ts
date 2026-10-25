// Number / currency / token formatters (port of frontend/js/utils/format.js).
//
// Three flavours of "compact number" exist intentionally and are NOT unified:
//   - formatNumber: K/M/B with 1 decimal; preserves integers untouched.
//   - formatTokens: K/M/B with 2 decimals on B and M, 1 decimal on K (with
//     trailing zeros trimmed). Used where tokens are the primary value. The K
//     branch keeps the same granularity as formatNumber, so the same magnitude
//     does not read as "1K" on one card and "1.4K" on the next.
// formatAxisNumber / formatAxisCost are a third, axis-tick-only flavour: as
// short as possible (no trailing zeros) so ticks fit on narrow screens.
//
// formatCount is the fourth: an *exact* count with thousands separators, not a
// compact magnitude. It exists because a raw toLocaleString() renders per
// browser locale, so the same number reads differently for different users.
//
// Every formatter is pinned to APP_LOCALE. A number that renders one way for
// one user and another way for the next is a data-honesty bug, not a
// preference.

/** The locale every formatter renders in. Pinned, not read from the browser. */
export const APP_LOCALE = 'en-US';

export function formatNumber(num: number | null | undefined): string {
  if (num === null || num === undefined || Number.isNaN(num)) return '—';
  const abs = Math.abs(num);
  if (abs >= 1e9) return `${(num / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(num / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(num / 1e3).toFixed(1)}K`;
  if (Number.isInteger(num)) return num.toString();
  return num.toFixed(1);
}

const formatterCache = new Map<string, Intl.NumberFormat>();

function getCurrencyFormatter(currencyCode?: string | null): Intl.NumberFormat {
  const code = (currencyCode || 'USD').toUpperCase();
  const key = `${APP_LOCALE}|${code}`;
  let f = formatterCache.get(key);
  if (!f) {
    try {
      f = new Intl.NumberFormat(APP_LOCALE, {
        style: 'currency',
        currency: code,
        currencyDisplay: 'narrowSymbol',
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    } catch {
      f = new Intl.NumberFormat(APP_LOCALE, {
        style: 'currency',
        currency: 'USD',
        currencyDisplay: 'narrowSymbol',
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    }
    formatterCache.set(key, f);
  }
  return f;
}

export function formatCurrency(
  amount: number | null | undefined,
  currencyCode?: string | null,
): string {
  if (amount === null || amount === undefined || Number.isNaN(amount)) return '—';
  return getCurrencyFormatter(currencyCode).format(amount);
}

// Token formatter. Zero renders as literal '0' (not '—') because detail
// panes show explicit zero rows.
export function formatTokens(val: number | null | undefined): string {
  if (val == null || val === 0) return '0';
  if (val >= 1e9) return (val / 1e9).toFixed(2) + 'B';
  if (val >= 1e6) return (val / 1e6).toFixed(2) + 'M';
  // One decimal, trailing zeros trimmed: 1.4K reads "1.4K" rather than "1K",
  // and 15000 reads "15K" rather than "15.0K".
  if (val >= 1e3) return trimTrailingZero((val / 1e3).toFixed(1)) + 'K';
  return String(val);
}

function trimTrailingZero(value: string): string {
  return value.endsWith('.0') ? value.slice(0, -2) : value;
}

// Exact count with thousands separators: 1500 renders "1,500", not "1.5K".
// This is the formatter for counts a user may need to read precisely —
// messages, tool calls, findings — where a compact magnitude would lose
// information.
const countFormatter = new Intl.NumberFormat(APP_LOCALE);

export function formatCount(count: number | null | undefined): string {
  if (count === null || count === undefined || Number.isNaN(count)) return '—';
  return countFormatter.format(count);
}

// Cost formatter. Distinguishes 0 (literal $0.00) from sub-cent (< $0.01)
// from null/missing (—) — all three render differently in cost panes.
export function formatCost(usd: number | null | undefined, currencyCode = 'USD'): string {
  if (usd == null) return '—';
  const f = getCurrencyFormatter(currencyCode);
  if (usd === 0) return f.format(0);
  if (usd > 0 && usd < 0.01) return '< ' + f.format(0.01);
  return f.format(usd);
}

// Percentage formatter. A share never rounds up onto 100%: 99.6% is
// "99.6%", not "100%" — a quota that is nearly exhausted must not read as one
// that is finished.
//
// The invariant is unconditional. An explicit `digits` sets the precision, not
// a licence to claim 100%: it is the floor the value is rendered at, and the
// widening below applies on top of it.
export function formatPct(pct: number | null | undefined, digits?: number): string {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return '—';
  const places = digits ?? 0;
  if (pct < 100 && Number(pct.toFixed(places)) >= 100) {
    // The rounding lands on the ceiling. Widen the precision until the value
    // reads as what it is — up to ten extra decimals, which is enough for
    // any percentage a quota is derived from (a billion-token quota is
    // 99.9999999% used at 999,999,999 tokens).
    for (let extra = 1; extra <= 10; extra += 1) {
      const widened = places + extra;
      if (Number(pct.toFixed(widened)) < 100) return `${pct.toFixed(widened)}%`;
    }
    // Past ten decimals the value is within a rounding error of 100, and no
    // display precision says what it is. A bound says it honestly:
    // ">99.99%" is nearly exhausted, where "100%" is finished.
    return '>99.99%';
  }
  return `${pct.toFixed(places)}%`;
}

// Compact duration for countdowns: "3d 4h", "4h 12m", "12m", "<1m".
export function formatDuration(ms: number): string {
  if (ms <= 0) return 'now';
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return '<1m';
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  if (hours > 0) return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  return `${mins}m`;
}

// "4h 12m" until an ISO timestamp, or null when missing/past-invalid.
export function timeUntil(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const target = Date.parse(iso);
  if (Number.isNaN(target)) return null;
  return formatDuration(target - Date.now());
}

// Relative "ago" formatter for last-seen / updated-at stamps.
export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '—';
  const ms = Date.now() - then;
  if (ms < 60_000) return 'just now';
  return `${formatDuration(ms)} ago`;
}

const axisFormatterCache = new Map<string, Intl.NumberFormat>();

function getAxisFormatter(currencyCode?: string): Intl.NumberFormat {
  const key = `${APP_LOCALE}|${currencyCode ?? ''}`;
  let f = axisFormatterCache.get(key);
  if (!f) {
    const opts: Intl.NumberFormatOptions = {
      notation: 'compact',
      minimumFractionDigits: 0,
      maximumFractionDigits: 1,
    };
    try {
      f = new Intl.NumberFormat(
        APP_LOCALE,
        currencyCode
          ? { ...opts, style: 'currency', currency: currencyCode, currencyDisplay: 'narrowSymbol' }
          : opts,
      );
    } catch {
      // Invalid currency code: deliberately fall back to USD (cached under the
      // requested key, as getCurrencyFormatter does) rather than throwing mid-render.
      f = new Intl.NumberFormat('en-US', { ...opts, style: 'currency', currency: 'USD' });
    }
    axisFormatterCache.set(key, f);
  }
  return f;
}

// Compact axis tick: 300M, 1.2B, 40K.
export function formatAxisNumber(v: number): string {
  return getAxisFormatter().format(v);
}

// Compact currency axis tick: $300, $1.2K.
export function formatAxisCost(v: number, currencyCode = 'USD'): string {
  return getAxisFormatter(currencyCode.toUpperCase()).format(v);
}
